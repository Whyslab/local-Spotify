"""Радио: бесконечная очередь от трека или артиста.

Наружу тесты не ходят: похожих артистов отвечает подменённый
``similar.similar_artists``, треки со стороны — подменённый
``outside.candidates`` (conftest по умолчанию глушит его в пустоту).
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from adder import config, db, library, navidrome, outside, radio, runtime, shuffle, similar

KEY = "0123456789abcdef"


# ---------------------------------------------------------------------------
# Скачивания со стороны: не больше K в час и не во время своих задач
# ---------------------------------------------------------------------------


@pytest.fixture
def running():
    """Как в test_outside.py: поток скачивания живёт, пока идёт тест, и
    останавливается в конце — иначе он доставался бы следующему тесту."""
    runtime.shutdown_event.clear()
    yield
    worker = outside._worker
    if worker is not None and worker.is_alive():
        runtime.shutdown_event.set()
        worker.join(timeout=5)
        assert not worker.is_alive(), "the outside fetch thread did not stop"
    outside._worker = None
    runtime.shutdown_event.clear()
    runtime.PROCESSING_URLS.clear()


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def remember(*keys):
    outside.remember(
        [{"key": key, "artist": "Markul", "title": key, "duration": 180} for key in keys]
    )


def fake_fetch(fetched):
    def fetch(key):
        fetched.append(key)
        outside._audio_path(key).write_bytes(b"audio")
        outside._update_meta(key, status="ready", fetched=time.time())

    return fetch


def test_no_more_than_the_hourly_limit_is_fetched(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "FETCHES_PER_HOUR", 3)
    keys = [f"{i:016x}" for i in range(5)]
    remember(*keys)

    outside.request(keys)
    assert wait_for(lambda: not outside._pending)

    assert len(fetched) == 3
    # Не скачанное сверх лимита — не неудача: его попросят снова, когда час
    # освободится, и оно скачается.
    assert sorted(outside.status(k) for k in keys) == ["ready"] * 3 + ["wanted"] * 2


def test_the_hour_frees_up_as_old_fetches_age(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "FETCHES_PER_HOUR", 1)
    outside._fetch_times.append(time.monotonic() - 3601)
    remember(KEY)

    outside.request([KEY])
    assert wait_for(lambda: outside.status(KEY) == "ready")


def test_nothing_is_fetched_while_the_users_own_downloads_run(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "USER_WAIT_STEP", 0.02)
    runtime.PROCESSING_URLS.add("https://www.youtube.com/watch?v=users_own")
    remember(KEY)

    outside.request([KEY])
    time.sleep(0.3)
    assert fetched == []
    assert outside.status(KEY) == "pending"

    runtime.PROCESSING_URLS.clear()
    assert wait_for(lambda: fetched == [KEY])


def test_a_keep_by_hand_does_not_wait_for_the_hour(monkeypatch, running):
    fetched: list[str] = []
    monkeypatch.setattr(outside, "fetch", fake_fetch(fetched))
    monkeypatch.setattr(outside, "FETCHES_PER_HOUR", 0)
    remember(KEY, "f" * 16)

    outside.request(["f" * 16])
    outside.request([KEY], keep=True)
    assert wait_for(lambda: not outside._pending)
    assert fetched == [KEY]


def test_a_keep_of_a_failed_track_leaves_no_mark(monkeypatch, running):
    remember(KEY)
    outside._update_meta(KEY, status="failed", failed_at=time.time())
    assert outside.request([KEY], keep=True) == {KEY: "failed"}
    assert outside._asked_by_hand == set()


def test_one_fetch_at_a_time(monkeypatch, running):
    """Больше двух разом нельзя по плану; поток скачивания один — значит, один."""
    inside = 0
    most = 0
    lock = threading.Lock()

    def slow_fetch(key):
        nonlocal inside, most
        with lock:
            inside += 1
            most = max(most, inside)
        time.sleep(0.05)
        with lock:
            inside -= 1

    monkeypatch.setattr(outside, "fetch", slow_fetch)
    keys = [f"{i:016x}" for i in range(4)]
    remember(*keys)
    outside.request(keys[:2])
    outside.request(keys[2:])
    assert wait_for(lambda: not outside._pending)
    assert most == 1


# ---------------------------------------------------------------------------
# Порция радио
# ---------------------------------------------------------------------------


def rows(artist, n):
    return [
        {"path": f"{artist}/Singles/{artist}{i}.m4a", "artist": artist, "title": f"{artist}{i}"}
        for i in range(n)
    ]


LIBRARY_ROWS = (
    rows("Seed", 6) + rows("Near1", 6) + rows("Near2", 6) + rows("Far1", 10) + rows("Far2", 10)
)
NEIGHBOURS = {"Seed": ["Near1", "Near2", "Nobody"], "Near1": ["Near3"], "Near2": []}


@pytest.fixture
def deezer(monkeypatch):
    monkeypatch.setattr(
        similar, "similar_artists", lambda artist, cache_dir, limit=12: NEIGHBOURS.get(artist, [])
    )


def library_tracks(library_rows=LIBRARY_ROWS):
    return shuffle.tracks_from_rows(library_rows, [], [], 12)


def artists(local):
    return {t.artist for t in local}


def test_the_station_keeps_to_the_artist_and_its_neighbours(deezer):
    local, found = radio.build(library_tracks(), "Seed", [], size=15)
    assert len(local) == 15
    assert found == []
    assert artists(local) <= {"Seed", "Near1", "Near2"}


def test_an_artists_radio_starts_with_the_artist(deezer):
    for _ in range(10):
        local, _ = radio.build(library_tracks(), "Seed", [], size=5)
        assert local[0].artist == "Seed"


def test_an_artists_radio_starts_with_the_artist_even_if_skipped_lately(deezer):
    tracks = library_tracks()
    skipped = {t.path for t in tracks if t.artist == "Seed"}
    local, _ = radio.build(tracks, "Seed", [], size=12, skipped=skipped)
    assert local[0].artist == "Seed"
    # Только первый: дальше пропущенное по-прежнему не ставится.
    assert not {t.path for t in local[1:]} & skipped


def test_nothing_from_the_last_fifty_comes_back(deezer):
    tracks = library_tracks()
    recent = [t.path for t in tracks if t.artist in ("Seed", "Near1")]
    local, _ = radio.build(tracks, "Seed", recent, previous=recent[-1], size=15)
    assert not {t.path for t in local} & set(recent)


def test_only_the_last_fifty_count_as_recent(deezer):
    tracks = library_tracks()
    old = tracks[0].path
    recent = [old] + [f"gone/{i}.m4a" for i in range(radio.WINDOW)]
    seen = set()
    for _ in range(30):
        local, _ = radio.build(tracks, "Seed", recent, size=18)
        seen |= {t.path for t in local}
    assert old in seen


def test_a_portion_goes_on_from_the_track_before_it(deezer):
    tracks = [
        shuffle.Track(path=f"x{i}", artist="Seed" if i < 10 else "Near1", tempo=80 + 10 * i)
        for i in range(20)
    ]
    for _ in range(10):
        local, _ = radio.build(tracks, "Seed", ["x0"], previous="x0", size=5)
        assert "x0" not in {t.path for t in local}
        # Первый в порции — сосед «x0» по темпу, а не любой трек круга.
        assert shuffle.tempo_distance(80, local[0].tempo) <= shuffle.TEMPO_TOLERANCE


def test_a_portion_after_a_track_from_outside_goes_on_from_the_last_library_one(deezer):
    tracks = [
        shuffle.Track(path=f"x{i}", artist="Seed" if i < 10 else "Near1", tempo=80 + 10 * i)
        for i in range(20)
    ]
    tail = outside.PREFIX + KEY
    for _ in range(10):
        local, _ = radio.build(tracks, "Seed", ["x5", tail], previous=tail, size=5)
        # Не «радио артиста с начала»: продолжение за x5 (130 BPM), а не за
        # первым попавшимся треком Seed.
        assert shuffle.tempo_distance(130, local[0].tempo) <= shuffle.TEMPO_TOLERANCE


def test_skipped_tracks_of_the_artist_do_not_come_back_mid_radio(deezer):
    tracks = library_tracks()
    skipped = {t.path for t in tracks if t.artist == "Seed"}
    # Порция кончилась треком со стороны — так и было, когда пропущенное
    # возвращалось: сервер принимал продолжение за начало радио артиста.
    heard = [tracks[-1].path, outside.PREFIX + KEY]
    for _ in range(10):
        local, _ = radio.build(tracks, "Seed", heard, previous=heard[-1], size=12, skipped=skipped)
        assert not {t.path for t in local} & skipped


def test_skipped_tracks_are_left_out(deezer):
    tracks = library_tracks()
    skipped = {t.path for t in tracks if t.artist == "Near1"}
    for _ in range(10):
        local, _ = radio.build(tracks, "Seed", [], size=12, skipped=skipped)
        assert not {t.path for t in local} & skipped


def test_a_small_circle_reaches_neighbours_of_neighbours_then_the_library(deezer):
    small = rows("Seed", 2) + rows("Near1", 2) + rows("Near3", 3) + rows("Far1", 20)
    tracks = library_tracks(small)

    names = radio.station("Seed", 10, tracks)
    assert "near3" in names

    local, _ = radio.build(tracks, "Seed", [], size=10)
    assert len(local) == 10
    # Сначала круг целиком, остальное — из фонотеки.
    assert sum(1 for t in local if t.artist != "Far1") == 7


def candidates_from(keys):
    def fake(seed_artists, have, want, budget=0):
        return [
            {"key": key, "artist": f"New{i}", "title": f"N{i}", "duration": 180, "seed": "Seed"}
            for i, key in enumerate(keys)
        ][:want]

    return fake


def test_about_a_third_is_new_and_what_was_heard_lately_is_not(deezer, monkeypatch):
    keys = [f"{i:016x}" for i in range(30)]
    monkeypatch.setattr(outside, "candidates", candidates_from(keys))
    heard = [outside.PREFIX + keys[0], outside.PREFIX + keys[1]]

    local, found = radio.build(library_tracks(), "Seed", heard, size=20, have=set())

    assert len(found) == 7
    assert len(local) == 13
    assert not {f["key"] for f in found} & {keys[0], keys[1]}


def test_too_few_new_tracks_are_made_up_with_our_own(deezer, monkeypatch):
    monkeypatch.setattr(outside, "candidates", candidates_from(["a" * 16, "b" * 16]))
    local, found = radio.build(library_tracks(), "Seed", [], size=20, have=set())
    assert len(found) == 2
    assert len(local) == 18


# ---------------------------------------------------------------------------
# Маршрут
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch, deezer):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(library, "library_index", lambda: LIBRARY_ROWS)

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_radio_from_an_artist(client, monkeypatch):
    monkeypatch.setattr(outside, "candidates", candidates_from([f"{i:016x}" for i in range(30)]))
    body = client.post("/api/radio", json={"artist": "Seed"}).json()

    assert body["artist"] == "Seed"
    assert len(body["queue"]) == 20
    assert body["external"] == 7
    first = body["queue"][0]
    assert first["artist"] == "Seed" and not first.get("external")
    new = [e for e in body["queue"] if e.get("external")]
    # Записаны — плеер может попросить их скачать.
    assert outside.status(outside.track_key(new[0]["path"])) == "wanted"


def test_radio_from_a_track_takes_its_artist_and_leaves_the_track_out(client):
    path = "Near1/Singles/Near13.m4a"
    body = client.post("/api/radio", json={"path": path, "recent": [path]}).json()

    assert body["artist"] == "Near1"
    assert path not in {e["path"] for e in body["queue"]}
    # Deezer по Near1 знает только Near3, которого в фонотеке нет: круг мал,
    # и порция добирается из фонотеки, но всё равно полная.
    assert len(body["queue"]) == 20


@pytest.mark.parametrize(
    "request_body, status",
    [
        ({}, 400),
        ({"path": "Seed/Singles/Seed0.m4a", "artist": "Seed"}, 400),
        ({"path": "Nobody/Singles/x.m4a"}, 404),
        ({"path": "outside:" + KEY}, 404),
        ({"path": "outside:../../etc/passwd"}, 404),
        ({"artist": "Seed", "recent": ["x"] * 51}, 422),
        ({"artist": "Seed", "size": 51}, 422),
        ({"artist": "   "}, 400),
    ],
)
def test_bad_radio_requests_are_refused(client, request_body, status):
    assert client.post("/api/radio", json=request_body).status_code == status


def test_a_skip_keeps_a_track_off_the_radio_until_it_is_heard_through(client):
    skipped, heard = "Seed/Singles/Seed0.m4a", "Seed/Singles/Seed1.m4a"
    for path, flags in ((skipped, [False, True]), (heard, [True, False])):
        for n, skip in enumerate(flags):
            client.post(
                "/api/plays",
                json={
                    "path": path,
                    "played_seconds": 10 if skip else 180,
                    "duration": 180,
                    "skipped": skip,
                    "mode": "radio",
                    "heard_at": time.time() - 600 + n * 200,
                },
            ).raise_for_status()

    assert radio.recently_skipped() == {skipped}
    # Пропуск старше двух недель трек уже не держит.
    db.db_exec(
        "INSERT INTO plays(path, played_at, skipped) "
        "VALUES('Seed/Singles/Seed2.m4a', datetime('now', 'localtime', '-15 days'), 1)"
    )
    assert radio.recently_skipped() == {skipped}
