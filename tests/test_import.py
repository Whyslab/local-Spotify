"""Getting music in from somewhere other than a YouTube link."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, ingest, library, navidrome, playlists, runtime, sources


def make_audio(path, seconds=2, title=None, artist=None):
    """A real encoded file, so the integrity check has something honest to read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        str(seconds),
    ]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    # Файл без обложки ищет её в iTunes — в тестах сети нет (см. conftest).
    monkeypatch.setattr(ingest, "get_hd_cover", lambda artist, title: (None, None))
    config.LIBRARY.mkdir(parents=True)
    runtime.TMP_DIR.mkdir(parents=True)
    runtime.PROCESSING_URLS.clear()
    library.invalidate_library_index()
    return tmp_path


@pytest.fixture()
def client(env):
    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


# ---------------------------------------------------------------------------
# Reading and checking files of every format the library accepts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".opus", ".m4a"])
def test_tags_are_read_from_every_accepted_format(env, suffix):
    """One reader, four containers -- callers should not have to know which."""
    path = make_audio(env / f"track{suffix}", title="Ночь", artist="Артист")
    meta = library.read_tags(path)

    assert meta["title"] == "Ночь"
    assert "Артист" in meta["artist"]
    assert meta["duration"] == pytest.approx(2.0, abs=0.3)


def test_a_truncated_file_is_rejected(env):
    """The failure a header check cannot see: good start, no end."""
    whole = make_audio(env / "whole.mp3", seconds=5)
    broken = env / "broken.mp3"
    broken.write_bytes(whole.read_bytes()[: len(whole.read_bytes()) // 3])

    ok, _ = ingest.validate_audio_integrity(whole)
    assert ok

    ok, message = ingest.validate_audio_integrity(broken)
    assert not ok
    assert message


def test_a_noisy_but_playable_file_is_accepted(env):
    """ffmpeg grumbles about plenty that does not make a file unplayable.

    Requiring silence from it would reject legitimate mp3s, so the rule is the
    exit code plus a list of messages that actually mean broken audio.
    """
    path = make_audio(env / "noisy.mp3", seconds=2)
    with open(path, "rb") as handle:
        original = handle.read()
    # A stray block of junk before the first frame: real files pick these up.
    path.write_bytes(b"\x00" * 512 + original)

    ok, message = ingest.validate_audio_integrity(path)
    assert ok, message


# ---------------------------------------------------------------------------
# Import endpoint
# ---------------------------------------------------------------------------


def test_uploading_a_file_queues_it(client, env):
    path = make_audio(env / "upload.mp3", title="Песня", artist="Кто-то")

    response = client.post(
        "/api/import",
        files={"files": ("upload.mp3", path.read_bytes(), "audio/mpeg")},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["accepted"]) == 1
    assert body["skipped"] == []


def test_the_same_bytes_twice_are_one_task(client, env):
    """A file has no URL, so it is keyed by its own content instead."""
    path = make_audio(env / "twice.mp3")
    payload = path.read_bytes()

    first = client.post("/api/import", files={"files": ("a.mp3", payload, "audio/mpeg")})
    second = client.post("/api/import", files={"files": ("b.mp3", payload, "audio/mpeg")})

    assert len(first.json()["accepted"]) == 1
    assert second.json()["accepted"] == []
    assert "already" in second.json()["skipped"][0]["reason"]


def test_an_unsupported_format_is_reported_not_swallowed(client, env):
    response = client.post(
        "/api/import",
        files={"files": ("notes.txt", b"this is not audio", "text/plain")},
    )

    body = response.json()
    assert body["accepted"] == []
    assert "unsupported format" in body["skipped"][0]["reason"]


def test_import_requires_auth(client, env):
    client.headers.pop("Authorization")
    response = client.post("/api/import", files={"files": ("a.mp3", b"x", "audio/mpeg")})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Naming an imported file
# ---------------------------------------------------------------------------


def test_names_come_from_the_tags_when_there_are_tags(env):
    """A file that arrives tagged knows better than its filename does."""
    from adder import db

    db.db_init()
    db.db_exec("INSERT INTO tasks(id, url, status) VALUES(1, 'file:x', 'queued')")
    path = make_audio(env / "whatever-the-file-is-called.mp3", title="Ночь", artist="Артист")

    result = ingest.import_local_file(1, path, path.name)

    assert result.names.meta_title == "Ночь"
    assert result.names.full_artist == "Артист"


@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_names_fall_back_to_the_filename(env, dash):
    from adder import db

    db.db_init()
    db.db_exec("INSERT INTO tasks(id, url, status) VALUES(1, 'file:y', 'queued')")
    # A hyphen, an en dash or an em dash: the same convention as YouTube titles.
    path = make_audio(env / f"Кино {dash} Группа крови.mp3")

    result = ingest.import_local_file(1, path, path.name)

    assert result.names.full_artist == "Кино"
    assert result.names.meta_title == "Группа крови"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_a_match_needs_artist_title_and_length_to_agree(monkeypatch):
    """Two of the three is what pulled in live versions and covers in August."""
    monkeypatch.setattr(
        sources,
        "search_youtube",
        lambda query, limit=8: [
            {"url": "https://y/1", "title": "Monday (Live)", "channel": "ROCKET", "duration": 240},
            {"url": "https://y/2", "title": "Monday", "channel": "ROCKET", "duration": 169},
        ],
    )

    match = sources.best_youtube_match(sources.Candidate("ROCKET", "Monday", 168.9))
    assert match["url"] == "https://y/2"


def test_no_match_is_better_than_a_wrong_one(monkeypatch):
    monkeypatch.setattr(
        sources,
        "search_youtube",
        lambda query, limit=8: [
            {"url": "https://y/1", "title": "Monday", "channel": "ROCKET", "duration": 400},
        ],
    )

    assert sources.best_youtube_match(sources.Candidate("ROCKET", "Monday", 169)) is None


def test_spotify_links_are_recognised():
    assert (
        sources.spotify_playlist_id("https://open.spotify.com/playlist/3HmDyWuVf4ahwj") is not None
    )
    assert sources.spotify_playlist_id("https://open.spotify.com/track/abc") is None
    assert sources.spotify_playlist_id("https://youtube.com/playlist?list=x") is None


def test_a_truncated_spotify_playlist_says_so(client, monkeypatch):
    """The embed page carries no total, so the message cannot be "100 of N"."""
    many = [sources.Candidate(f"A{i}", f"T{i}", 100) for i in range(sources.SPOTIFY_EMBED_LIMIT)]
    monkeypatch.setattr(sources, "spotify_playlist", lambda url: (many, True))
    monkeypatch.setattr(sources, "best_youtube_match", lambda candidate: None)

    body = client.post(
        "/api/import-playlist",
        json={"url": "https://open.spotify.com/playlist/3HmDyWuVf4ahwjDLLvovv1"},
    ).json()

    assert body["truncated"] is True
    assert body["read"] == sources.SPOTIFY_EMBED_LIMIT
    assert str(sources.SPOTIFY_EMBED_LIMIT) in body["note"]


def test_unmatched_playlist_tracks_are_reported(client, monkeypatch):
    """A track that cannot be found must show up in a report, not vanish."""
    monkeypatch.setattr(
        sources,
        "spotify_playlist",
        lambda url: ([sources.Candidate("Кто-то", "Редкий трек", 200)], False),
    )
    monkeypatch.setattr(sources, "best_youtube_match", lambda candidate: None)

    body = client.post(
        "/api/import-playlist",
        json={"url": "https://open.spotify.com/playlist/abc"},
    ).json()

    assert body["queued"] == 0
    assert body["unmatched"] == [{"artist": "Кто-то", "title": "Редкий трек"}]


def test_a_youtube_playlist_queues_every_video(client, monkeypatch):
    monkeypatch.setattr(
        sources,
        "youtube_playlist",
        lambda url: [
            "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        ],
    )

    body = client.post(
        "/api/import-playlist",
        json={"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
    ).json()

    assert body["source"] == "youtube"
    assert body["queued"] == 2


def test_search_passes_results_through(client, monkeypatch):
    monkeypatch.setattr(
        sources,
        "search_youtube",
        lambda q, limit=8: [{"url": "https://y/1", "title": "T", "channel": "C", "duration": 10}],
    )
    body = client.get("/api/search", params={"q": "что-нибудь"}).json()
    assert body["results"][0]["title"] == "T"


# ---------------------------------------------------------------------------
# Полный путь загрузки: /api/import → обработка → фонотека
# ---------------------------------------------------------------------------


def test_an_untagged_upload_keeps_its_real_filename_all_the_way(client, env):
    """Файл без тегов раньше ложился в фонотеку под хешем: имя терялось на
    полпути, в папке импорта он назван по содержимому."""
    from adder import db
    from adder import queue as task_queue

    path = make_audio(env / "src" / "Кино - Группа крови.mp3")
    body = client.post(
        "/api/import", files={"files": (path.name, path.read_bytes(), "audio/mpeg")}
    ).json()
    tid = body["accepted"][0]["task"]
    url = db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))[0]["url"]

    task_queue.process(tid, url)

    task = db.db_query("SELECT status, result_path FROM tasks WHERE id = ?", (tid,))[0]
    assert task["status"] == "done", task
    assert task["result_path"] == "Кино/Singles/Группа крови.mp3"
    # Загруженный файл и его имя убраны только после успеха.
    assert ingest.stashed_upload(url) is None and ingest.stashed_name(url) is None


def test_an_upload_survives_an_interrupted_attempt(client, env, monkeypatch):
    """Перенос файла в обработку терял его при перезапуске посреди работы."""
    from adder import db
    from adder import queue as task_queue

    path = make_audio(env / "src" / "Артист - Песня.mp3")
    body = client.post(
        "/api/import", files={"files": (path.name, path.read_bytes(), "audio/mpeg")}
    ).json()
    tid = body["accepted"][0]["task"]
    url = db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))[0]["url"]

    real = ingest.ingest_temp_file

    def interrupted(*args, **kwargs):
        raise runtime.ShutdownRequested()

    monkeypatch.setattr(ingest, "ingest_temp_file", interrupted)
    task_queue.process(tid, url)
    assert ingest.stashed_upload(url) is not None

    monkeypatch.setattr(ingest, "ingest_temp_file", real)
    task_queue.process(tid, url)
    assert db.db_query("SELECT status FROM tasks WHERE id = ?", (tid,))[0]["status"] == "done"


def test_a_retried_link_does_not_revive_an_old_replacement(client, env):
    """Упавшая «замени A на X», а потом обычное добавление X, уносило A в корзину."""
    from adder import db

    db.db_exec(
        "INSERT INTO tasks(url, status, replace_of) VALUES(?, 'error', 'A/Singles/a.m4a')",
        ("https://www.youtube.com/watch?v=abcdefghijk",),
    )
    client.post("/api/add", json={"links": ["https://www.youtube.com/watch?v=abcdefghijk"]})
    task = db.db_query("SELECT status, replace_of FROM tasks")[0]
    assert task["status"] == "queued"
    assert task["replace_of"] is None


def test_a_deleted_track_can_be_added_again_with_the_same_link(client, env):
    from adder import db

    link = "https://www.youtube.com/watch?v=abcdefghijk"
    db.db_exec(
        "INSERT INTO tasks(url, status, result_path) VALUES(?, 'done', 'A/Singles/gone.m4a')",
        (link,),
    )
    added = client.post("/api/add", json={"links": [link]}).json()["added"]
    assert len(added) == 1
    # А трек, который на месте, второй раз не качается.
    make_audio(config.LIBRARY / "A" / "Singles" / "here.m4a")
    db.db_exec(
        "INSERT INTO tasks(url, status, result_path) VALUES(?, 'done', 'A/Singles/here.m4a')",
        ("https://www.youtube.com/watch?v=bbbbbbbbbbb",),
    )
    again = client.post(
        "/api/add", json={"links": ["https://www.youtube.com/watch?v=bbbbbbbbbbb"]}
    ).json()
    assert again["added"] == []


def test_a_crafted_delete_path_still_lands_in_the_trash(env):
    target = make_audio(config.LIBRARY / "A" / "Singles" / "t.m4a")
    crafted = "A/Singles/../Singles/t.m4a"
    monkeypatch_guard = runtime.guard_real_library
    try:
        runtime.guard_real_library = lambda *a, **k: None
        result = library.delete_track(crafted)
    finally:
        runtime.guard_real_library = monkeypatch_guard
    assert not target.exists()
    assert (runtime.TRASH_DIR / "A" / "Singles" / "t.m4a").exists(), result


@pytest.mark.parametrize(
    "sent", ["./A/Singles/x.m4a", "A//Singles/x.m4a", "A/Singles/../Singles/x.m4a"]
)
def test_a_replacement_remembers_the_path_the_playlists_use(client, env, sent):
    """Подборки хранят «A/Singles/x.m4a»; «./A/…» не совпало бы ни с одной строкой."""
    from adder import db

    make_audio(config.LIBRARY / "A" / "Singles" / "x.m4a")
    response = client.post(
        "/api/replace", json={"path": sent, "url": "https://www.youtube.com/watch?v=abcdefghijk"}
    )
    upload = make_audio(env / "right.mp3", seconds=3)
    by_file = client.post(
        "/api/replace-file",
        params={"path": sent},
        files={"file": ("right.mp3", upload.read_bytes(), "audio/mpeg")},
    )
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
        runtime.TASK_QUEUE.task_done()

    assert response.status_code == by_file.status_code == 200, (response.text, by_file.text)
    rows = db.db_query("SELECT replace_of FROM tasks ORDER BY id")
    assert [r["replace_of"] for r in rows] == ["A/Singles/x.m4a"] * 2


def test_the_same_upload_stashed_at_once_is_not_an_error(env):
    """Две одинаковые загрузки писали один «<хеш>.part», и второй replace падал."""
    import threading

    payload = b"\x00" * 2_000_000
    errors = []
    for _ in range(10):
        barrier = threading.Barrier(4)

        def one(barrier=barrier):
            barrier.wait()
            try:
                ingest.stash_upload(payload, "a.mp3")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert not errors, errors[:3]
    assert [p.name for p in ingest.import_dir().iterdir() if ".part" in p.name] == []


def test_an_upload_is_written_and_queued_off_the_event_loop(client, env, monkeypatch):
    """Запись и sha256 двухсот мегабайт и ожидание FILE_LOCK в async-обработчике
    останавливали цикл событий — а с ним и отдачу музыки."""
    import asyncio

    from adder import app as app_module

    on_loop = []

    def running_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    real_stash, real_queue = ingest.stash_upload, app_module._queue_source

    def stash(*args, **kwargs):
        on_loop.append(("stash", running_loop()))
        return real_stash(*args, **kwargs)

    def queue_source(*args, **kwargs):
        on_loop.append(("queue", running_loop()))
        return real_queue(*args, **kwargs)

    monkeypatch.setattr(ingest, "stash_upload", stash)
    monkeypatch.setattr(app_module, "_queue_source", queue_source)
    make_audio(config.LIBRARY / "A" / "Singles" / "x.m4a")
    upload = make_audio(env / "u.mp3").read_bytes()
    other = make_audio(env / "v.mp3", seconds=3).read_bytes()

    client.post("/api/import", files={"files": ("u.mp3", upload, "audio/mpeg")})
    client.post(
        "/api/replace-file",
        params={"path": "A/Singles/x.m4a"},
        files={"file": ("v.mp3", other, "audio/mpeg")},
    )
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
        runtime.TASK_QUEUE.task_done()

    assert on_loop == [("stash", False), ("queue", False)] * 2


def test_a_stashed_upload_keeps_the_usual_permissions(env):
    """copy2 переносит права дальше, в фонотеку: файл 0600 от mkstemp
    Navidrome — другой пользователь — прочесть бы не смог."""
    _, stashed = ingest.stash_upload(b"audio", "a.mp3")
    usual = ingest.import_dir() / "usual"
    usual.write_bytes(b"x")

    assert stashed.stat().st_mode & 0o777 == usual.stat().st_mode & 0o777


def test_an_upload_is_readable_by_other_users_like_navidrome(tmp_path, monkeypatch):
    # mkstemp would give 0600, and copy2 carries the mode into the library,
    # where Navidrome (another user) could not read the track.
    import os

    from adder import ingest

    monkeypatch.setattr(ingest.runtime, "TMP_DIR", tmp_path)
    old = os.umask(0o022)
    try:
        _, stored = ingest.stash_upload(b"audio bytes", "song.mp3")
    finally:
        os.umask(old)
    assert stored.stat().st_mode & 0o777 == 0o644
    assert not list(stored.parent.glob(".*.part"))
