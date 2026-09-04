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


def test_names_fall_back_to_the_filename(env):
    from adder import db

    db.db_init()
    db.db_exec("INSERT INTO tasks(id, url, status) VALUES(1, 'file:y', 'queued')")
    path = make_audio(env / "Кино - Группа крови.mp3")

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
