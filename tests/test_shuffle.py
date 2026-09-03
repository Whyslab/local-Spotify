"""Building a queue with a shape: smooth transitions, spread artists, wide coverage."""

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, db, library, navidrome, playlists, runtime, shuffle


def track(path, artist, tempo=None, energy=0.5, rank=None, affinity=0.0):
    return shuffle.Track(
        path=path,
        artist=artist,
        tempo=tempo,
        energy=energy,
        last_play_rank=rank,
        hour_affinity=affinity,
    )


# ---------------------------------------------------------------------------
# Tempo
# ---------------------------------------------------------------------------


def test_half_and_double_time_are_the_same_groove():
    """80 next to 160 is not a jump, and treating it as one splits the library."""
    assert shuffle.tempo_distance(80, 160) == 0
    assert shuffle.tempo_distance(160, 80) == 0
    # 90 doubles to 180, which is 40 from 140 -- closer than 90 itself is.
    assert shuffle.tempo_distance(90, 140) == pytest.approx(40)
    assert shuffle.tempo_distance(100, 104) == pytest.approx(4)


def test_an_unmeasured_track_is_never_excluded_by_the_tempo_filter():
    """It must stay reachable -- the weighting, not the filter, is what demotes it."""
    assert shuffle.tempo_distance(None, 120) == 0
    assert shuffle.tempo_distance(120, None) == 0


def test_an_unknown_transition_is_worth_less_than_a_smooth_one():
    """Treating "no idea" as "perfectly smooth" is what made the feature inert."""
    measured = track("a.m4a", "A", tempo=120)
    same = track("b.m4a", "B", tempo=121)
    unknown = track("c.m4a", "C")

    assert shuffle._transition_weight(measured, unknown) < shuffle._transition_weight(
        measured, same
    )


# ---------------------------------------------------------------------------
# The queue itself -- criteria 24 and 25
# ---------------------------------------------------------------------------


def test_adjacent_tracks_stay_within_the_tempo_threshold():
    """Criterion 24: no more than 12 BPM between neighbours."""
    tracks = [track(f"t{i}.m4a", f"Artist {i}", tempo=90 + i) for i in range(40)]
    tracks += [track(f"f{i}.m4a", f"Other {i}", tempo=160 + i) for i in range(40)]

    queue = shuffle.build_queue(tracks, size=30, seed=7)
    report = shuffle.queue_report(queue)

    assert report["tracks"] == 30
    assert report["measured_transitions"] == 29
    assert report["max_tempo_jump"] <= shuffle.TEMPO_TOLERANCE


def test_the_same_artist_does_not_follow_itself():
    """Criterion 25. Three tracks each by ten artists, thirty in the queue."""
    tracks = [
        track(f"{artist}-{i}.m4a", artist, tempo=100 + i)
        for artist in [f"Artist {n}" for n in range(10)]
        for i in range(3)
    ]

    queue = shuffle.build_queue(tracks, size=30, seed=3)

    assert shuffle.queue_report(queue)["artist_repeats"] == 0


def test_a_library_of_one_artist_still_produces_a_queue():
    """The repeat rule is a weight, not a ban: otherwise this would deadlock."""
    tracks = [track(f"t{i}.m4a", "Only Artist", tempo=100) for i in range(10)]

    queue = shuffle.build_queue(tracks, size=10, seed=1)

    assert len(queue) == 10


def test_recently_played_tracks_come_back_less_often():
    """The other half of the complaint: the same hundred tracks, forever."""
    fresh = [track(f"fresh{i}.m4a", f"A{i}", tempo=100) for i in range(50)]
    stale = [track(f"stale{i}.m4a", f"B{i}", tempo=100, rank=0) for i in range(50)]

    picked_stale = 0
    for seed in range(30):
        queue = shuffle.build_queue(fresh + stale, size=10, seed=seed)
        picked_stale += sum(1 for t in queue if t.path.startswith("stale"))

    # Uniform choice would give about half; the weighting should push this well
    # below that without ever making a track unreachable.
    assert picked_stale < 30 * 10 * 0.35


def test_never_played_tracks_are_reachable():
    tracks = [track(f"t{i}.m4a", f"A{i}", tempo=100) for i in range(20)]
    seen = set()
    for seed in range(40):
        seen.update(t.path for t in shuffle.build_queue(tracks, size=5, seed=seed))
    assert len(seen) == 20


def test_the_hour_shifts_the_odds_when_there_is_a_journal():
    plain = [track(f"p{i}.m4a", f"A{i}", tempo=100) for i in range(20)]
    evening = [track(f"e{i}.m4a", f"B{i}", tempo=100, affinity=1.0) for i in range(20)]

    with_moment = sum(
        sum(1 for t in shuffle.build_queue(plain + evening, size=8, seed=s) if t.path[0] == "e")
        for s in range(25)
    )
    without = sum(
        sum(
            1
            for t in shuffle.build_queue(plain + evening, size=8, seed=s, use_moment=False)
            if t.path[0] == "e"
        )
        for s in range(25)
    )

    assert with_moment > without


def test_an_empty_library_is_an_empty_queue():
    assert shuffle.build_queue([], size=10) == []


def test_the_queue_never_repeats_a_track():
    tracks = [track(f"t{i}.m4a", f"A{i}", tempo=100 + i) for i in range(30)]
    queue = shuffle.build_queue(tracks, size=30, seed=5)
    assert len({t.path for t in queue}) == 30


# ---------------------------------------------------------------------------
# Assembling the inputs
# ---------------------------------------------------------------------------


def test_recency_and_hour_come_out_of_the_journal():
    index = [
        {"path": "a.m4a", "artist": "A"},
        {"path": "b.m4a", "artist": "B"},
        {"path": "c.m4a", "artist": "C"},
    ]
    features = [{"path": "a.m4a", "tempo": 120.0, "energy": 0.4, "brightness": 2000.0}]
    # Newest first, which is what "recently" is measured against.
    plays = [
        {"path": "b.m4a", "hour": 22},
        {"path": "a.m4a", "hour": 9},
        {"path": "b.m4a", "hour": 22},
    ]

    tracks = {t.path: t for t in shuffle.tracks_from_rows(index, features, plays, current_hour=22)}

    assert tracks["a.m4a"].tempo == 120.0
    assert tracks["b.m4a"].last_play_rank == 0  # the most recent play
    assert tracks["a.m4a"].last_play_rank == 1
    assert tracks["c.m4a"].last_play_rank is None  # never played
    assert tracks["b.m4a"].hour_affinity > 0  # both of its plays were at this hour
    assert tracks["a.m4a"].hour_affinity == 0


def test_no_journal_means_no_hour_affinity():
    index = [{"path": "a.m4a", "artist": "A"}]
    tracks = shuffle.tracks_from_rows(index, [], [], current_hour=3)
    assert tracks[0].hour_affinity == 0
    assert tracks[0].last_play_rank is None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True)
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [
            {"path": f"A{i}/Singles/t{i}.m4a", "artist": f"A{i}", "title": f"T{i}", "duration": 180}
            for i in range(20)
        ],
    )

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_shuffle_endpoint_returns_a_queue_and_its_numbers(client):
    body = client.get("/api/shuffle", params={"size": 10}).json()

    assert len(body["queue"]) == 10
    assert body["report"]["tracks"] == 10
    assert body["total"] == 20
    assert body["analysed"] == 0  # nothing measured yet in this fixture


def test_plain_mode_is_available_for_comparison(client):
    body = client.get("/api/shuffle", params={"size": 5, "mode": "plain"}).json()
    assert body["mode"] == "plain"
    assert len(body["queue"]) == 5


def test_a_blind_trial_does_not_leak_which_side_is_which(client):
    body = client.post("/api/shuffle/blind", params={"size": 6}).json()

    assert set(body) == {"trial", "A", "B"}
    assert "smart" not in str(body).lower()
    assert len(body["A"]) == 6 and len(body["B"]) == 6


def test_a_blind_trial_records_one_answer(client):
    trial = client.post("/api/shuffle/blind", params={"size": 4}).json()["trial"]

    assert client.post(f"/api/shuffle/blind/{trial}", json={"choice": "A"}).status_code == 200
    # A second answer to the same trial would be a second vote for one listen.
    assert client.post(f"/api/shuffle/blind/{trial}", json={"choice": "B"}).status_code == 409
    assert client.post(f"/api/shuffle/blind/{trial}", json={"choice": "C"}).status_code == 400


def test_blind_results_apply_the_stated_threshold(client, monkeypatch):
    """Criterion 27: the smart queue chosen in at least 7 of 10."""
    for _ in range(10):
        trial = client.post("/api/shuffle/blind", params={"size": 4}).json()
        side = db.db_query("SELECT smart_side FROM blind_trials WHERE id = ?", (trial["trial"],))[
            0
        ]["smart_side"]
        client.post(f"/api/shuffle/blind/{trial['trial']}", json={"choice": side})

    results = client.get("/api/shuffle/blind/results").json()
    assert results["decided"] == 10
    assert results["smart_chosen"] == 10
    assert results["passes"] is True


def test_an_unmeasured_queue_reports_no_jump_rather_than_a_zero_one():
    """0.0 would read as "perfectly smooth"; the truth is "nothing to compare"."""
    tracks = [track(f"t{i}.m4a", f"A{i}") for i in range(5)]

    report = shuffle.queue_report(shuffle.build_queue(tracks, size=5, seed=1))

    assert report["max_tempo_jump"] is None
    assert report["measured_transitions"] == 0


def test_measured_tracks_are_over_represented_but_do_not_take_over():
    """The balance the design is actually after, before the analyser has finished.

    Measured tracks should appear far more often than their share, because they
    are the ones the queue can be steered by -- otherwise the smart shuffle is
    the plain shuffle until an hour of CPU has run. They should not crowd the
    rest out either: covering the shelf is the other half of the complaint, and
    a track cannot be discovered while it is waiting to be measured.

    Twenty measured out of two hundred is a tenth of the library, so uniform
    choice would give about twenty of the two hundred picks below.
    """
    measured = [track(f"m{i}.m4a", f"M{i}", tempo=100 + (i % 5)) for i in range(20)]
    unmeasured = [track(f"u{i}.m4a", f"U{i}") for i in range(180)]

    picked = sum(
        sum(1 for t in shuffle.build_queue(measured + unmeasured, size=10, seed=s) if t.tempo)
        for s in range(20)
    )

    assert picked > 40, "measured tracks are not favoured enough to steer the queue"
    assert picked < 150, "measured tracks are crowding out everything unheard"


def test_an_unmeasured_track_is_still_reachable():
    """A preference, not a filter: coverage still matters for what has no tempo."""
    measured = [track(f"m{i}.m4a", f"M{i}", tempo=100) for i in range(5)]
    unmeasured = [track(f"u{i}.m4a", f"U{i}") for i in range(5)]

    seen = set()
    for seed in range(40):
        seen.update(t.path for t in shuffle.build_queue(measured + unmeasured, size=6, seed=seed))

    assert any(p.startswith("u") for p in seen)
