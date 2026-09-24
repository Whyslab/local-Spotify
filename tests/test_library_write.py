"""Writing into the Navidrome library: process() end to end, offline.

Only the network is faked (yt-dlp, Deezer, cover downloads). The audio is a
real one-second AAC file, so tags are written and read back with the same
mutagen code Navidrome's scanner relies on, and the file is really moved into
the library layout Navidrome watches.
"""

import shutil
import sys
import time
from pathlib import Path

import pytest
from mutagen.mp4 import MP4, MP4Cover

from adder import config, db, enrich, ingest, library, runtime
from adder import queue as adder_queue

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
URL = "https://www.youtube.com/watch?v=abc123"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
    monkeypatch.setattr(ingest, "check_disk_space", lambda *a, **k: (True, 10_000))
    runtime.TMP_DIR.mkdir()
    config.LIBRARY.mkdir()
    runtime.PROCESSING_URLS.clear()
    runtime.shutdown_event.clear()
    library.invalidate_library_index()
    db.db_init()
    return ingest


def youtube(app, monkeypatch, meta, audio=FIXTURE):
    """Fake yt-dlp: metadata from `meta`, and `audio` as the downloaded file."""
    monkeypatch.setattr(ingest, "yt_meta", lambda url: {"id": "abc123", **meta})

    def download(url, vid):
        target = runtime.TMP_DIR / f"{vid}.m4a"
        if isinstance(audio, bytes):
            target.write_bytes(audio)
        else:
            shutil.copy(audio, target)
        return target

    monkeypatch.setattr(ingest, "yt_download", download)


def deezer(app, monkeypatch, info):
    """Fake enrichment: `info` as a Deezer hit, or None for "not found"."""
    monkeypatch.setattr(enrich, "lookup", lambda artist, title: info)


def covers(app, monkeypatch, deezer_cover=None, fallback_cover=None):
    requested = []

    def by_url(url):
        requested.append(url)
        return (deezer_cover, "jpg") if deezer_cover else (None, None)

    def by_search(artist, title, thumb):
        requested.append(thumb)
        return (fallback_cover, "png") if fallback_cover else (None, None)

    monkeypatch.setattr(ingest, "fetch_cover_url", by_url)
    monkeypatch.setattr(ingest, "fetch_cover", by_search)
    return requested


def run(app, url=URL):
    tid = db.db_exec("INSERT INTO tasks(url, status) VALUES(?, 'queued')", (url,)).lastrowid
    adder_queue.process(tid, url)
    return db.db_query("SELECT * FROM tasks WHERE id = ?", (tid,))[0]


def library_files(app):
    return sorted(p.relative_to(config.LIBRARY) for p in config.LIBRARY.rglob("*.m4a"))


def test_deezer_match_is_tagged_and_filed_for_navidrome(app, monkeypatch, caplog):
    youtube(app, monkeypatch, {"title": "Daft Punk - Get Lucky (Official Video)", "uploader": "x"})
    deezer(
        app,
        monkeypatch,
        enrich.TrackInfo(
            album="Random Access Memories",
            artists=["Daft Punk", "Pharrell Williams"],
            track_number=8,
            track_total=13,
            date="2013-05-17",
            cover_url="https://deezer/cover.jpg",
        ),
    )
    requested = covers(app, monkeypatch, deezer_cover=JPEG)

    with caplog.at_level("INFO"):
        task = run(app)

    assert task["status"] == "done", task["error"]
    assert (task["artist"], task["title"]) == ("Daft Punk", "Get Lucky")
    assert library_files(app) == [Path("Daft Punk/Singles/Get Lucky.m4a")]

    tags = MP4(config.LIBRARY / "Daft Punk/Singles/Get Lucky.m4a").tags
    assert tags["\xa9nam"] == ["Get Lucky"]
    assert tags["\xa9ART"] == ["Daft Punk", "Pharrell Williams"]  # one value per artist
    assert tags["aART"] == ["Daft Punk"]
    assert tags["\xa9alb"] == ["Random Access Memories"]
    assert tags["\xa9day"] == ["2013-05-17"]
    assert tags["trkn"] == [(8, 13)]
    assert tags["disk"] == [(1, 0)]  # disc 1 of an unknown number
    assert bytes(tags["covr"][0]) == JPEG
    assert tags["covr"][0].imageformat == MP4Cover.FORMAT_JPEG

    assert requested == ["https://deezer/cover.jpg"]  # no fallback needed
    assert list(runtime.TMP_DIR.iterdir()) == []
    assert any("Task finished: stored" in r.getMessage() for r in caplog.records)


def test_unknown_track_becomes_its_own_single_with_the_fallback_cover(app, monkeypatch):
    youtube(
        app,
        monkeypatch,
        {"title": "Rare Song (Live)", "uploader": "Small Band", "thumbnail": "https://yt/t.jpg"},
    )
    deezer(app, monkeypatch, None)
    requested = covers(app, monkeypatch, fallback_cover=PNG)

    task = run(app)

    assert task["status"] == "done", task["error"]
    # The version stays in the tag, not in the filename.
    assert library_files(app) == [Path("Small Band/Singles/Rare Song.m4a")]
    tags = MP4(config.LIBRARY / "Small Band/Singles/Rare Song.m4a").tags
    assert tags["\xa9nam"] == ["Rare Song (Live)"]
    assert tags["\xa9alb"] == ["Rare Song (Live)"]
    assert tags["\xa9ART"] == ["Small Band"]
    assert "trkn" not in tags
    assert tags["covr"][0].imageformat == MP4Cover.FORMAT_PNG
    assert requested == ["https://yt/t.jpg"]


def test_track_without_any_cover_is_still_added(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    task = run(app)

    assert task["status"] == "done", task["error"]
    assert "covr" not in MP4(config.LIBRARY / "A/Singles/B.m4a").tags


def test_same_name_different_audio_is_kept_beside_the_first(app, monkeypatch):
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    run(app, "https://www.youtube.com/watch?v=first")

    # Same name, different bytes: tagging a second copy changes nothing about
    # its audio, so make the audio itself differ.
    other = runtime.TMP_DIR.parent / "other.m4a"
    shutil.copy(FIXTURE, other)
    audio = MP4(other)
    audio["\xa9cmt"] = ["different"]
    audio.save()
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"}, audio=other)
    run(app, "https://www.youtube.com/watch?v=second")

    assert library_files(app) == [Path("A/Singles/B (1).m4a"), Path("A/Singles/B.m4a")]


def test_corrupt_download_never_reaches_the_library(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"}, audio=b"<html>not audio</html>")
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    task = run(app)

    assert task["status"] == "error"
    assert "M4A validation failed" in task["error"]
    assert library_files(app) == []
    assert list(runtime.TMP_DIR.iterdir()) == []


def test_unavailable_video_fails_once_without_retrying(app, monkeypatch):
    calls = []

    def unavailable(url):
        calls.append(url)
        raise RuntimeError("ERROR: [youtube] abc123: Private video. Sign in if you've been granted")

    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    monkeypatch.setattr(ingest, "yt_meta", unavailable)

    task = run(app)

    assert task["status"] == "error"
    assert task["error_type"] == "youtube_not_found"
    assert len(calls) == 1
    assert library_files(app) == []


def test_rate_limit_is_retried_then_succeeds(app, monkeypatch):
    attempts = []

    def throttled_once(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("ERROR: [youtube] abc123: HTTP Error 429: Too Many Requests")
        return {"id": "abc123", "title": "A - B", "uploader": "x"}

    youtube(app, monkeypatch, {})
    monkeypatch.setattr(ingest, "yt_meta", throttled_once)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    monkeypatch.setattr(runtime.shutdown_event, "wait", lambda seconds: False)  # no real backoff
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    task = run(app)

    assert len(attempts) == 2
    assert task["status"] == "done", task["error"]
    assert library_files(app) == [Path("A/Singles/B.m4a")]


def test_bot_check_error_tells_the_user_what_to_set(app, monkeypatch):
    def bot_check(url):
        raise RuntimeError("ERROR: [youtube] abc123: Sign in to confirm you're not a bot.")

    monkeypatch.setattr(ingest, "yt_meta", bot_check)

    task = run(app)

    assert task["error_type"] == "youtube_auth_required"
    assert "COOKIES_FROM_BROWSER" in task["error"]
    assert len(task["error"]) <= 300


def test_new_track_is_searchable_immediately(app, monkeypatch):
    library.library_index()  # warm the 60-second cache while the library is empty
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    run(app)

    assert [row["title"] for row in library.library_index()] == ["B"]


# ---------------------------------------------------------------------------
# The same song from a second video: kept, but flagged
# ---------------------------------------------------------------------------


def _task(tid):
    return db.db_query("SELECT * FROM tasks WHERE id = ?", (tid,))[0]


def test_same_song_from_another_video_is_kept_with_a_warning(app, monkeypatch, caplog):
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    youtube(app, monkeypatch, {"title": "Artist - Song (Official Video)", "uploader": "x"})
    first = run(app, "https://www.youtube.com/watch?v=clip")

    other = runtime.TMP_DIR.parent / "other.m4a"
    shutil.copy(FIXTURE, other)
    audio = MP4(other)
    audio["\xa9cmt"] = ["lyric video upload"]
    audio.save()
    youtube(
        app, monkeypatch, {"title": "Artist - Song (Lyric Video)", "uploader": "x"}, audio=other
    )
    with caplog.at_level("WARNING"):
        second = run(app, "https://www.youtube.com/watch?v=lyrics")

    assert first["status"] == second["status"] == "done"
    assert not first["warning"]
    assert library_files(app) == [
        Path("Artist/Singles/Song (1).m4a"),
        Path("Artist/Singles/Song.m4a"),
    ]
    assert second["warning"] == "Похоже на уже имеющийся трек: Artist/Singles/Song.m4a"
    assert any("similar track" in r.getMessage() for r in caplog.records)


def test_a_live_version_is_not_flagged(app, monkeypatch):
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    youtube(app, monkeypatch, {"title": "Artist - Song", "uploader": "x"})
    run(app, "https://www.youtube.com/watch?v=studio")

    other = runtime.TMP_DIR.parent / "live.m4a"
    shutil.copy(FIXTURE, other)
    audio = MP4(other)
    audio["\xa9cmt"] = ["live"]
    audio.save()
    youtube(app, monkeypatch, {"title": "Artist - Song (Live)", "uploader": "x"}, audio=other)
    live = run(app, "https://www.youtube.com/watch?v=live")

    assert live["status"] == "done"
    assert not live["warning"]


def test_a_different_length_is_not_the_same_recording(app, monkeypatch):
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [
            {
                "path": "A/Singles/B.m4a",
                "artist": "A",
                "albumartist": "A",
                "title": "B",
                "duration": 240.0,
            }
        ],
    )

    assert ingest.find_similar_library_track(["A"], "B", 239.0) == "A/Singles/B.m4a"
    assert ingest.find_similar_library_track(["A"], "B", 180.0) is None
    assert ingest.find_similar_library_track(["Someone Else"], "B", 240.0) is None
    assert ingest.find_similar_library_track(["A"], "B", None) == "A/Singles/B.m4a"


def test_resubmitting_clears_an_old_warning(app):
    tid = db.db_exec(
        "INSERT INTO tasks(url, status, warning) VALUES('u', 'error', 'old')"
    ).lastrowid

    from adder import app as app_module

    app_module._reset_task(tid)

    assert not _task(tid)["warning"]


# ---------------------------------------------------------------------------
# Rate limiting: a long backoff, shared by every worker
# ---------------------------------------------------------------------------


def test_rate_limit_backs_off_for_a_minute_and_pauses_youtube(app, monkeypatch):
    waits = []

    def throttled_once(url):
        if not waits:
            raise RuntimeError("ERROR: [youtube] abc123: HTTP Error 429: Too Many Requests")
        return {"id": "abc123", "title": "A - B", "uploader": "x"}

    def record_wait(seconds):
        waits.append((seconds, runtime.youtube_pause_left()))
        return False

    youtube(app, monkeypatch, {})
    monkeypatch.setattr(ingest, "yt_meta", throttled_once)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_BACKOFF", 60.0)
    monkeypatch.setattr(runtime.shutdown_event, "wait", record_wait)
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    task = run(app)

    assert task["status"] == "done", task["error"]
    ((backoff, pause_left),) = waits
    assert backoff == 60.0
    assert 55 < pause_left <= 60  # the other workers are held too


def test_yt_dlp_waits_out_a_rate_limit_pause(app):
    runtime.pause_youtube(0.4)

    started = time.monotonic()
    result = ingest.run_yt_dlp([sys.executable, "-c", "pass"], timeout=10)

    assert result.returncode == 0
    assert time.monotonic() - started >= 0.35


def test_shutdown_interrupts_a_rate_limit_pause(app):
    runtime.pause_youtube(30)
    runtime.shutdown_event.set()
    try:
        with pytest.raises(runtime.ShutdownRequested):
            ingest.run_yt_dlp([sys.executable, "-c", "pass"], timeout=10)
    finally:
        runtime.shutdown_event.clear()


# ---------------------------------------------------------------------------
# Live streams and over-long videos are refused before downloading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meta",
    [
        {"is_live": True},
        {"live_status": "is_upcoming"},
        {"duration": 31 * 60},
    ],
)
def test_unsuitable_videos_are_refused_without_downloading(app, monkeypatch, meta):
    downloads = []
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x", **meta})
    monkeypatch.setattr(ingest, "yt_download", lambda *a: downloads.append(a))
    monkeypatch.setattr(config, "MAX_DURATION_MINUTES", 30)
    monkeypatch.setattr(config, "MAX_RETRIES", 3)

    task = run(app)

    assert task["status"] == "error"
    assert task["error_type"] == "unsupported_video"
    assert downloads == []


def test_duration_limit_can_be_switched_off(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x", "duration": 5 * 3600})
    monkeypatch.setattr(config, "MAX_DURATION_MINUTES", 0)
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    assert run(app)["status"] == "done"


# ---------------------------------------------------------------------------
# Free space is checked where the file is going, not only where it lands first
# ---------------------------------------------------------------------------


def test_a_full_library_disk_stops_the_download(tmp_path, monkeypatch):
    library_disk = tmp_path / "library-disk"
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "LIBRARY", library_disk / "Music")
    monkeypatch.setattr(config, "MIN_FREE_SPACE_MB", 100)
    runtime.TMP_DIR.mkdir()
    library_disk.mkdir()

    def usage(path):
        free = 10 if Path(path).is_relative_to(library_disk) else 10_000
        return shutil._ntuple_diskusage(0, 0, free * 1024 * 1024)

    monkeypatch.setattr(ingest.shutil, "disk_usage", usage)

    assert ingest.check_disk_space() == (False, 10)


def test_a_finished_link_is_released_so_it_can_be_added_again(app, monkeypatch):
    # After a successful task the URL stayed in PROCESSING_URLS, and /api/add
    # skips anything in that set: deleting the track and re-adding the link
    # (which the API allows) was silently ignored until a restart.
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    runtime.PROCESSING_URLS.add(URL)

    assert run(app)["status"] == "done"
    assert URL not in runtime.PROCESSING_URLS


def test_a_duplicate_result_also_releases_the_link(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    run(app, "https://www.youtube.com/watch?v=first")

    runtime.PROCESSING_URLS.add(URL)
    run(app)  # byte-identical audio: stored as a duplicate, not a new file

    assert URL not in runtime.PROCESSING_URLS


def test_second_disc_is_not_written_as_2_of_1(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(
        app,
        monkeypatch,
        enrich.TrackInfo(album="Double", artists=["A"], track_number=3, disc_number=2),
    )
    covers(app, monkeypatch)

    run(app)

    assert MP4(config.LIBRARY / "A/Singles/B.m4a").tags["disk"] == [(2, 0)]


# ---------------------------------------------------------------------------
# The source link travels inside the file
# ---------------------------------------------------------------------------


def test_the_video_link_is_written_into_the_file(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)

    run(app)

    assert library.read_tags(config.LIBRARY / "A/Singles/B.m4a")["source"] == URL
    library.invalidate_library_index()
    assert library.find_by_source(URL) == "A/Singles/B.m4a"


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".opus"])
def test_the_link_round_trips_in_every_format(tmp_path, suffix):
    import subprocess

    path = tmp_path / f"t{suffix}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(FIXTURE), str(path)], check=True)

    ingest.write_source(path, URL)

    assert library.read_tags(path)["source"] == URL


def test_a_link_already_in_the_library_is_not_downloaded_again(app, monkeypatch):
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    run(app)

    # The task table is lost (a new database), the file is still there.
    runtime.DB_PATH.unlink()
    db.db_init()
    library.invalidate_library_index()
    downloads = []
    monkeypatch.setattr(ingest, "yt_meta", lambda url: downloads.append(url))

    task = run(app)

    assert downloads == []
    assert task["status"] == "done"
    assert task["result_path"] == "A/Singles/B.m4a"
    assert URL not in runtime.PROCESSING_URLS


def test_a_finished_and_a_failed_task_reach_the_desktop(app, monkeypatch):
    from adder import notify

    added, failed = [], []
    monkeypatch.setattr(notify, "track_added", lambda a, t: added.append((a, t)))
    monkeypatch.setattr(notify, "track_failed", lambda n, r: failed.append((n, r)))
    youtube(app, monkeypatch, {"title": "A - B", "uploader": "x"})
    deezer(app, monkeypatch, None)
    covers(app, monkeypatch)
    run(app)

    def private(url):
        raise RuntimeError("ERROR: [youtube] x: Private video")

    monkeypatch.setattr(ingest, "yt_meta", private)
    run(app, "https://www.youtube.com/watch?v=gone")

    assert added == [("A", "B")]
    assert failed == [("https://www.youtube.com/watch?v=gone", "видео недоступно")]
