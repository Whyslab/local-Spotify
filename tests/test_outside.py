"""Треки не из фонотеки в умном перемешивании.

Наружу тесты не ходят: Deezer подменяется через `similar`, YouTube — через
`outside._search` и `outside._download`. Настоящий подбор (`candidates`)
возвращается там, где он проверяется, — conftest по умолчанию его глушит.
"""

import time

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, library, navidrome, outside, playlists, runtime, similar

REAL_CANDIDATES = outside.candidates
KEY = "0123456789abcdef"


# ---------------------------------------------------------------------------
# Путь в очереди
# ---------------------------------------------------------------------------


def test_only_a_strict_key_is_accepted_after_the_prefix():
    assert outside.track_key("outside:" + KEY) == KEY
    for bad in (
        "outside:../../etc/passwd",
        "outside:0123456789ABCDEF",
        "outside:0123",
        "outside:" + KEY + "/x",
        "outside:",
        KEY,
        "Artist/Album/track.m4a",
    ):
        assert outside.track_key(bad) is None, bad


def test_the_key_does_not_depend_on_case_or_featured_guests():
    assert outside.key_for("Markul", "Fata Morgana") == outside.key_for(
        "markul • Oxxxymiron", "fata morgana"
    )


# ---------------------------------------------------------------------------
# Выбор версии на YouTube
# ---------------------------------------------------------------------------


def video(vid, title, duration, channel="Someone"):
    return {"id": vid, "title": title, "duration": duration, "channel": channel}


def test_the_version_is_chosen_by_length_not_by_popularity():
    """Официальный клип первым в выдаче, но он длиннее на вставку."""
    found = outside.choose_video(
        [
            video("clip", "Markul feat Oxxxymiron - FATA MORGANA (2017)", 227),
            video("audio", "Markul ft Oxxxymiron FATA MORGANA Audio", 184),
        ],
        "Markul",
        "Fata Morgana",
        183,
    )
    assert found["id"] == "audio"


@pytest.mark.parametrize(
    "title",
    [
        "Fata Morgana (Караоке)",
        "Fata Morgana | Live at Booking Machine Festival",
        "American Reacts To Fata Morgana",
        "Fata Morgana (sped up)",
        "Fata Morgana cover",
    ],
)
def test_other_versions_of_the_same_length_are_refused(title):
    assert outside.choose_video([video("x", title, 183)], "Markul", "Fata Morgana", 183) is None


def test_an_unwanted_word_hidden_inside_a_title_word_still_refuses():
    """«live» внутри «Alive» не разрешает концерт «Alive (Live at …)»."""
    entries = [video("x", "Alive (Live at Wembley)", 200)]
    assert outside.choose_video(entries, "Someone", "Alive", 200) is None


def test_a_word_from_the_title_itself_is_not_a_reason_to_refuse():
    """«Live Forever» — не концерт."""
    found = outside.choose_video(
        [video("ok", "Oasis - Live Forever (Official Audio)", 276)], "Oasis", "Live Forever", 277
    )
    assert found["id"] == "ok"


def test_nothing_of_the_right_length_means_nothing_at_all():
    """Лучше пропустить трек, чем сыграть вместо него чужую версию."""
    assert (
        outside.choose_video([video("x", "Fata Morgana", 260)], "Markul", "Fata Morgana", 183)
        is None
    )


def test_a_video_about_another_song_is_refused_even_at_the_right_length():
    assert (
        outside.choose_video([video("x", "Markul - Серпантин", 183)], "Markul", "Fata Morgana", 183)
        is None
    )


def test_guests_and_producers_in_the_title_do_not_stop_a_match():
    found = outside.choose_video(
        [video("ok", "Boulevard Depo - Vozrast", 150)],
        "Boulevard Depo",
        "Vozrast (feat. Artur Kreem) (Prod. Babyboi)",
        151,
    )
    assert found["id"] == "ok"
    assert outside.core_title("Song feat. Someone") == "Song"
    assert outside.core_title("(feat. X)") == "(feat. X)"


def test_a_title_deezer_spells_in_latin_matches_a_cyrillic_video():
    found = outside.choose_video(
        [video("ok", "Boulevard Depo - Возраст (feat. Artur Kreem) | Official Audio", 138)],
        "Boulevard Depo",
        "Vozrast (feat. Artur Kreem) (Prod. Babyboi)",
        137,
    )
    assert found["id"] == "ok"


def test_the_artist_topic_channel_wins_even_under_a_bare_title():
    found = outside.choose_video(
        [
            video("fan", "Серёга - Возле дома твоего (текст)", 214, "rain i si"),
            video("topic", "Возле дома твоего", 214, "Серёга - Topic"),
        ],
        "Серёга",
        "Возле дома твоего",
        214,
    )
    assert found["id"] == "topic"


def test_without_a_known_length_only_official_audio_is_trusted():
    entries = [
        video("fan", "Markul - Fata Morgana", 190),
        video("off", "Markul - Fata Morgana (Audio)", 184),
    ]
    assert outside.choose_video(entries, "Markul", "Fata Morgana", None)["id"] == "off"


# ---------------------------------------------------------------------------
# Вплетение в очередь
# ---------------------------------------------------------------------------


def local(n):
    return [{"path": f"L{i}.m4a", "artist": f"A{i % 5}", "title": f"T{i}"} for i in range(n)]


def found(n, seed=lambda i: f"A{i % 5}"):
    return [
        {
            "key": f"{i:016x}",
            "artist": f"New{i}",
            "title": f"N{i}",
            "duration": 180,
            "seed": seed(i),
        }
        for i in range(n)
    ]


def test_about_every_third_track_is_new_and_the_first_is_always_ours():
    for attempt in range(30):
        queue = outside.weave(local(33), found(40), 17, seed=attempt)
        assert len(queue) == 50
        assert not queue[0].get("external")
        assert sum(1 for e in queue if e.get("external")) == 17


def test_two_new_tracks_never_stand_next_to_each_other():
    for attempt in range(30):
        queue = outside.weave(local(33), found(40), 17, seed=attempt)
        for first, second in zip(queue, queue[1:], strict=False):
            assert not (first.get("external") and second.get("external"))


def test_our_tracks_keep_their_order():
    queue = outside.weave(local(20), found(10), 10, seed=1)
    assert [e["path"] for e in queue if not e.get("external")] == [e["path"] for e in local(20)]


def test_a_new_track_follows_the_artist_it_was_found_for():
    queue = outside.weave(local(20), found(20), 8, seed=3)
    for before, item in zip(queue, queue[1:], strict=False):
        if item.get("external"):
            number = int(item["title"][1:])
            assert f"A{number % 5}" == before["artist"]


def test_new_entries_look_like_queue_rows_and_say_what_they_are():
    queue = outside.weave(local(3), found(1), 1, seed=0)
    new = next(e for e in queue if e.get("external"))
    assert new["path"] == "outside:" + f"{0:016x}"
    assert new["outside"] is True
    assert new["artist"] == "New0" and new["title"] == "N0"


def test_fewer_found_than_asked_is_fine():
    queue = outside.weave(local(10), found(2), 5, seed=0)
    assert sum(1 for e in queue if e.get("external")) == 2
    assert len(queue) == 12


# ---------------------------------------------------------------------------
# Подбор: Deezer подменён
# ---------------------------------------------------------------------------


@pytest.fixture
def deezer(monkeypatch):
    monkeypatch.setattr(outside, "candidates", REAL_CANDIDATES)
    related = {"Markul": ["Oxxxymiron", "Скриптонит"], "Баста": ["Гуф"]}
    tops = {
        "Markul": [{"artist": "Markul", "title": "Fata Morgana", "duration": 183}],
        "Oxxxymiron": [{"artist": "Oxxxymiron", "title": "Где нас нет", "duration": 250}],
        "Скриптонит": [{"artist": "Скриптонит", "title": "Это любовь", "duration": 200}],
        "Баста": [{"artist": "Баста", "title": "Сансара", "duration": 280}],
        "Гуф": [{"artist": "Гуф", "title": "Ice Baby", "duration": 230}],
    }
    monkeypatch.setattr(
        similar, "similar_artists", lambda name, cache, limit=12: related.get(name, [])
    )
    monkeypatch.setattr(similar, "top_tracks", lambda name, cache, limit=5: tops.get(name, []))


def test_candidates_skip_what_the_library_has_and_take_from_both_seeds(deezer):
    have = outside.library_keys([{"artist": "Markul", "title": "Fata Morgana"}])
    got = outside.candidates(["Markul", "Баста"], have, want=10)

    titles = {item["title"] for item in got}
    assert "Fata Morgana" not in titles
    assert {"Где нас нет", "Это любовь", "Сансара", "Ice Baby"} <= titles
    assert {item["seed"] for item in got} == {"Markul", "Баста"}
    assert all(item["duration"] for item in got)


def test_a_library_song_with_guests_in_its_title_is_not_new(deezer):
    """В фонотеке «Fata Morgana (feat. Oxxxymiron)», у Deezer — «Fata Morgana»."""
    have = outside.library_keys([{"artist": "Markul", "title": "Fata Morgana (feat. Oxxxymiron)"}])
    titles = {item["title"] for item in outside.candidates(["Markul"], have, want=10)}
    assert "Fata Morgana" not in titles


def test_a_track_youtube_did_not_have_is_not_offered_again(deezer):
    key = outside.key_for("Oxxxymiron", "Где нас нет")
    outside._update_meta(key, artist="Oxxxymiron", title="Где нас нет", status="failed")
    titles = {item["title"] for item in outside.candidates(["Markul"], set(), want=10)}
    assert "Где нас нет" not in titles


def test_candidates_stop_at_what_was_asked(deezer):
    assert len(outside.candidates(["Markul", "Баста"], set(), want=2)) == 2


def test_a_slow_deezer_does_not_hold_the_queue(monkeypatch):
    """Что не успело за отведённое время, просто не попадает в этот раз."""
    monkeypatch.setattr(outside, "candidates", REAL_CANDIDATES)

    def slow(name, cache, limit=12):
        time.sleep(2)
        return ["X"]

    monkeypatch.setattr(similar, "similar_artists", slow)
    monkeypatch.setattr(similar, "top_tracks", lambda *a, **kw: [])
    started = time.monotonic()
    assert outside.candidates(["Markul"], set(), want=5, budget=0.3) == []
    assert time.monotonic() - started < 1.5


def test_names_left_in_line_when_time_runs_out_are_dropped_not_crashed_on(monkeypatch):
    """Больше имён, чем потоков: не начатые отменяются, и подбор не падает."""
    monkeypatch.setattr(outside, "candidates", REAL_CANDIDATES)

    def slow(name, cache, limit=12):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(similar, "similar_artists", slow)
    monkeypatch.setattr(similar, "top_tracks", lambda *a, **kw: [])
    seeds = [f"Artist {i}" for i in range(12)]
    assert outside.candidates(seeds, set(), want=5, budget=0.2) == []


# ---------------------------------------------------------------------------
# Кэш: состояние, скачивание в фоне, уборка
# ---------------------------------------------------------------------------


def item(key=KEY, **extra):
    return {"key": key, "artist": "Markul", "title": "Fata Morgana", "duration": 183, **extra}


@pytest.fixture
def running():
    """Служба не останавливается: иначе фоновое скачивание сразу выходит.

    Выход TestClient из предыдущего теста оставляет shutdown_event поднятым.
    """
    runtime.shutdown_event.clear()
    yield
    runtime.shutdown_event.clear()


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_unknown_keys_are_not_fetched():
    assert outside.status(KEY) == "unknown"
    assert outside.request([KEY, "../../x"]) == {KEY: "unknown"}


def test_a_requested_track_is_fetched_in_the_background(monkeypatch, running):
    def fake_fetch(key):
        outside._audio_path(key).write_bytes(b"audio")
        outside._update_meta(key, status="ready", fetched=time.time())

    monkeypatch.setattr(outside, "fetch", fake_fetch)
    outside.remember([item()])
    assert outside.status(KEY) == "wanted"

    assert outside.request([KEY]) == {KEY: "pending"}
    assert wait_for(lambda: outside.status(KEY) == "ready")
    assert outside.audio_file(KEY) is not None


def test_a_track_youtube_does_not_have_is_marked_failed_and_not_retried(monkeypatch, running):
    monkeypatch.setattr(outside, "_search", lambda query: [])
    monkeypatch.setattr(outside.ingest, "check_disk_space", lambda: (True, 10_000))
    outside.remember([item()])

    outside.request([KEY])
    assert wait_for(lambda: outside.status(KEY) == "failed")
    assert "длины" in outside.read_meta(KEY)["reason"]
    # Второй раз не просит: неудача запомнена.
    assert outside.request([KEY]) == {KEY: "failed"}


def test_remember_does_not_reset_a_known_track():
    outside.remember([item()])
    outside._update_meta(KEY, status="failed", failed_at=time.time())
    outside.remember([item()])
    assert outside.read_meta(KEY)["status"] == "failed"


def test_cleanup_keeps_fresh_tracks_and_drops_old_ones():
    now = time.time()
    day = 24 * 3600
    cases = {
        "a" * 16: ({"status": "ready", "used": now - 2 * day}, True),
        "b" * 16: ({"status": "ready", "used": now - 31 * day, "fetched": now - 40 * day}, False),
        "c" * 16: ({"status": "failed", "failed_at": now - 8 * day}, False),
        "d" * 16: ({"status": "failed", "failed_at": now - 1 * day}, True),
        "e" * 16: ({"status": "wanted", "at": now - 8 * day}, False),
        "9" * 16: ({"status": "wanted", "at": now - 3 * day}, True),
    }
    for key, (meta, _) in cases.items():
        outside._update_meta(key, artist="A", title="T", **meta)
        if meta["status"] == "ready":
            outside._audio_path(key).write_bytes(b"audio")
    orphan = outside._audio_path("f" * 16)
    orphan.write_bytes(b"audio")
    # Идёт скачивание: у файла нет описания, но это не мусор.
    downloading = outside._dir() / ("8" * 16 + ".dl.m4a")
    downloading.write_bytes(b"half")

    outside.cleanup(now=now)

    for key, (_, kept) in cases.items():
        assert (outside.read_meta(key) is not None) is kept, key
    assert outside._audio_path("a" * 16).exists()
    assert not outside._audio_path("b" * 16).exists()
    assert not orphan.exists()
    assert downloading.exists()


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------


LIBRARY_ROWS = [
    {
        "path": f"A{i}/Singles/t{i}.m4a",
        "artist": f"A{i % 6}",
        "title": f"T{i}",
        "album": f"Al{i % 3}",
        "duration": 180,
    }
    for i in range(40)
]


@pytest.fixture
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
    monkeypatch.setattr(library, "library_index", lambda: LIBRARY_ROWS)

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


@pytest.fixture
def plenty(monkeypatch):
    """Deezer всегда находит, что подмешать."""

    def fake(seed_artists, have, want, budget=0):
        return [
            {
                "key": f"{i:016x}",
                "artist": f"New{i}",
                "title": f"N{i}",
                "duration": 180,
                "seed": seed_artists[0],
            }
            for i in range(want)
        ]

    monkeypatch.setattr(outside, "candidates", fake)


def test_smart_shuffle_mixes_in_about_a_third_of_new_tracks(client, plenty):
    body = client.get("/api/shuffle", params={"size": 30}).json()

    assert len(body["queue"]) == 30
    assert body["external"] == 10
    assert not body["queue"][0].get("external")
    new = [e for e in body["queue"] if e.get("external")]
    assert all(e["path"].startswith("outside:") for e in new)
    # Своё из фонотеки — не «находка»: костяка нет, со стороны только новое.
    assert all(e.get("external") for e in body["queue"] if e.get("outside"))
    # Кандидаты записаны — их можно попросить скачать.
    assert outside.status(outside.track_key(new[0]["path"])) == "wanted"


def test_plain_shuffle_never_gets_new_tracks(client, plenty):
    body = client.get("/api/shuffle", params={"size": 30, "mode": "plain"}).json()
    assert body["external"] == 0


def test_without_deezer_the_queue_is_whole_and_ours(client):
    """conftest глушит подбор: так выглядит очередь без сети."""
    body = client.get("/api/shuffle", params={"size": 30}).json()
    assert len(body["queue"]) == 30
    assert body["external"] == 0


def test_anything_can_be_shuffled_smartly(client, plenty):
    album = [row["path"] for row in LIBRARY_ROWS if row["album"] == "Al1"]
    body = client.post("/api/shuffle/smart", json={"paths": album, "size": 12}).json()

    assert body["core"] == len(album)
    assert body["queue"][0]["path"] in album
    assert body["external"] == 4
    ours = [e["path"] for e in body["queue"] if not e.get("external")]
    assert set(ours) <= set(album)


def test_shuffling_unknown_tracks_is_refused(client, plenty):
    response = client.post("/api/shuffle/smart", json={"paths": ["nope.m4a"]})
    assert response.status_code == 404


@pytest.fixture
def ready_track():
    outside.remember([item(album="Альбом")])
    outside._audio_path(KEY).write_bytes(b"not really audio but served as is")
    outside._update_meta(KEY, status="ready", fetched=time.time())
    return "outside:" + KEY


def test_a_ready_track_streams_like_a_library_one(client, ready_track):
    signed = client.get("/api/stream-url", params={"path": ready_track}).json()
    served = client.get(signed["url"])
    assert served.status_code == 200
    assert served.content.startswith(b"not really audio")


def test_a_track_that_is_not_downloaded_yet_is_not_streamed(client):
    outside.remember([item()])
    assert client.get("/api/stream-url", params={"path": "outside:" + KEY}).status_code == 404


def test_a_crafted_outside_path_is_refused(client):
    for path in ("outside:../../adder.db", "outside:ABCDEF0123456789"):
        assert client.get("/api/stream-url", params={"path": path}).status_code == 400
        assert client.get("/api/cover", params={"path": path}).status_code == 400


def test_track_details_come_from_deezer_and_have_no_measurements(client, ready_track):
    body = client.get("/api/track", params={"path": ready_track}).json()
    assert body["title"] == "Fata Morgana"
    assert body["album"] == "Альбом"
    assert body["external"] is True
    assert body["features"] is None


def test_lyrics_for_a_new_track_are_asked_by_its_tags(client, ready_track, monkeypatch):
    from adder import lyrics

    asked = {}

    def fake_ask(artist, title, album, duration):
        asked.update(artist=artist, title=title, duration=duration)
        return {"plainLyrics": "строка"}

    monkeypatch.setattr(lyrics, "CACHE_DIR", runtime.OUTSIDE_DIR / "lyrics")
    monkeypatch.setattr(lyrics, "_ask", fake_ask)
    body = client.get("/api/lyrics", params={"path": ready_track}).json()
    assert body["found"] is True
    assert asked == {"artist": "Markul", "title": "Fata Morgana", "duration": 183}


def test_status_and_prefetch_endpoints(client, monkeypatch):
    monkeypatch.setattr(outside, "fetch", lambda key: None)
    outside.remember([item()])
    assert client.get("/api/outside/status", params={"keys": KEY}).json() == {
        "status": {KEY: "wanted"}
    }
    answer = client.post("/api/outside/prefetch", json={"keys": [KEY, "bad"]}).json()
    assert answer == {"status": {KEY: "pending"}}


def test_keep_sends_the_file_through_the_normal_import(client, ready_track):
    from adder import db

    body = client.post(f"/api/outside/{KEY}/keep").json()
    assert body["queued"] is True
    task = db.db_query("SELECT url, status FROM tasks WHERE id = ?", (body["task"],))[0]
    assert task["url"].startswith("file:")
    assert task["status"] == "queued"
    # Второй раз — не второй импорт.
    assert client.post(f"/api/outside/{KEY}/keep").json()["queued"] is False


def test_keep_before_the_download_is_refused_but_starts_it(client, monkeypatch):
    monkeypatch.setattr(outside, "fetch", lambda key: None)
    outside.remember([item()])
    response = client.post(f"/api/outside/{KEY}/keep")
    assert response.status_code == 409
    assert response.json()["status"] == "pending"


def test_keep_says_when_youtube_did_not_have_it(client):
    outside.remember([item()])
    outside._update_meta(KEY, status="failed")
    assert client.post(f"/api/outside/{KEY}/keep").json()["status"] == "failed"


def test_a_short_find_does_not_shorten_the_queue(client, monkeypatch):
    def few(seed_artists, have, want, budget=0):
        return [
            {
                "key": f"{i:016x}",
                "artist": f"New{i}",
                "title": f"N{i}",
                "duration": 180,
                "seed": "A0",
            }
            for i in range(3)
        ]

    monkeypatch.setattr(outside, "candidates", few)
    body = client.get("/api/shuffle", params={"size": 30}).json()
    assert len(body["queue"]) == 30
    assert body["external"] == 3


def test_a_crashed_worker_does_not_block_later_downloads(monkeypatch, running):
    """Если поток упал целиком, следующая просьба запускает новый."""
    calls = []

    def fetch(key):
        calls.append(key)
        if key == KEY:
            raise RuntimeError("не скачался")
        outside._audio_path(key).write_bytes(b"audio")
        outside._update_meta(key, status="ready", fetched=time.time())

    real_update = outside._update_meta

    def update(key, **fields):
        if fields.get("status") == "failed":
            raise OSError("диск полон")
        return real_update(key, **fields)

    monkeypatch.setattr(outside, "fetch", fetch)
    monkeypatch.setattr(outside, "_update_meta", update)
    other = "fedcba9876543210"
    outside.remember([item(), item(key=other, title="Другая")])

    outside.request([KEY])
    assert wait_for(lambda: KEY in calls and outside.status(KEY) != "pending")
    outside.request([other])
    assert wait_for(lambda: outside.status(other) == "ready")
