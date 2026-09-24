"""The duplicates screen: two copies side by side, one kept, playlists follow."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from adder import config, db, duplicates, ingest, library, playlists, runtime
from adder import queue as adder_queue

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(runtime, "guard_real_library", lambda *a, **k: None)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(ingest, "check_disk_space", lambda *a, **k: (True, 10_000))
    monkeypatch.setattr(ingest, "fetch_cover", lambda *a: (None, None))
    runtime.TMP_DIR.mkdir()
    config.LIBRARY.mkdir()
    runtime.PROCESSING_URLS.clear()
    library.invalidate_library_index()
    db.db_init()
    return tmp_path


def add(env, monkeypatch, video, title, comment):
    """Run one download of the fixture, made byte-distinct by a comment tag."""
    source = env / f"{video}.m4a"
    shutil.copy(FIXTURE, source)
    audio = MP4(source)
    audio["\xa9cmt"] = [comment]
    audio.save()
    monkeypatch.setattr(
        ingest, "yt_meta", lambda url: {"id": video, "title": title, "uploader": "x"}
    )

    def download(url, vid):
        target = runtime.TMP_DIR / f"{vid}.m4a"
        shutil.copy(source, target)
        return target

    monkeypatch.setattr(ingest, "yt_download", download)
    url = f"https://www.youtube.com/watch?v={video}"
    tid = db.db_exec("INSERT INTO tasks(url, status) VALUES(?, 'queued')", (url,)).lastrowid
    adder_queue.process(tid, url)
    return tid


@pytest.fixture()
def pair(env, monkeypatch):
    add(env, monkeypatch, "clip", "Artist - Song (Official Video)", "clip")
    tid = add(env, monkeypatch, "lyrics", "Artist - Song (Lyric Video)", "lyrics")
    library.invalidate_library_index()
    return tid


def test_a_flagged_download_shows_up_as_a_pair(pair):
    (found,) = duplicates.pairs()

    assert found["task"] == pair
    assert found["new"]["path"] == "Artist/Singles/Song (1).m4a"
    assert found["existing"]["path"] == "Artist/Singles/Song.m4a"
    assert found["new"]["source"] == "https://www.youtube.com/watch?v=lyrics"
    assert found["new"]["codec"] and found["new"]["size"] > 0


def test_keeping_the_new_copy_puts_it_in_the_old_ones_playlist_places(pair):
    playlists.create("Mix", ["Artist/Singles/Song.m4a"])

    result = duplicates.resolve(pair, "new")

    assert result["removed"] == "Artist/Singles/Song.m4a"
    assert not (config.LIBRARY / "Artist/Singles/Song.m4a").exists()
    assert (runtime.TRASH_DIR / "Artist/Singles/Song.m4a").exists()
    assert [e.path for e in playlists.read("Mix").entries] == ["Artist/Singles/Song (1).m4a"]
    assert duplicates.pairs() == []


def test_keeping_the_old_copy_removes_the_new_one(pair):
    duplicates.resolve(pair, "existing")

    assert (config.LIBRARY / "Artist/Singles/Song.m4a").exists()
    assert not (config.LIBRARY / "Artist/Singles/Song (1).m4a").exists()
    task = db.db_query("SELECT result_path, warning FROM tasks WHERE id = ?", (pair,))[0]
    assert task == {"result_path": "Artist/Singles/Song.m4a", "warning": None}


def test_keeping_both_only_clears_the_question(pair):
    duplicates.resolve(pair, "both")

    assert (config.LIBRARY / "Artist/Singles/Song.m4a").exists()
    assert (config.LIBRARY / "Artist/Singles/Song (1).m4a").exists()
    assert duplicates.pairs() == []


def test_a_pair_whose_twin_was_deleted_meanwhile_goes_away(pair):
    library.delete_track("Artist/Singles/Song.m4a")
    library.invalidate_library_index()

    assert duplicates.pairs() == []
    assert db.db_query("SELECT warning FROM tasks WHERE id = ?", (pair,))[0]["warning"] is None


def test_the_api(pair):
    from adder import app as app_module

    with TestClient(app_module.app) as client:
        assert client.get("/api/duplicates").status_code == 401
        listed = client.get("/api/duplicates", headers=AUTH).json()
        assert [p["task"] for p in listed] == [pair]

        bad = client.post("/api/duplicates/resolve", json={"task": pair, "keep": "x"}, headers=AUTH)
        assert bad.status_code == 400
        missing = client.post(
            "/api/duplicates/resolve", json={"task": 999, "keep": "both"}, headers=AUTH
        )
        assert missing.status_code == 404

        done = client.post(
            "/api/duplicates/resolve", json={"task": pair, "keep": "new"}, headers=AUTH
        )
        assert done.status_code == 200
        assert client.get("/api/duplicates", headers=AUTH).json() == []
