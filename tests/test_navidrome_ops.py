"""Work owed to Navidrome that no file write can carry: full scans, queued ops."""

import shutil
import time
from pathlib import Path

import pytest

from adder import config, db, fix_covers, ingest, loudness, navidrome, sync

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"


@pytest.fixture()
def nd(monkeypatch):
    """Navidrome configured, its scan counted instead of sent."""
    monkeypatch.setattr(config, "NAVIDROME_USER", "u")
    monkeypatch.setattr(config, "NAVIDROME_PASSWORD", "p")
    db.db_init()
    scans: list[float] = []
    monkeypatch.setattr(navidrome, "full_scan", lambda: scans.append(time.time()))
    return scans


def _track(name: str = "t.m4a") -> Path:
    path = config.LIBRARY / "A" / "Singles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, path)
    return path


def test_tag_edits_reach_navidrome_as_one_full_scan_a_minute_later(nd):
    """The edit keeps the file's mtime, so Navidrome's quick scan skips it:
    only a full scan reads the new tags. Many edits in a row — one scan."""
    path = _track()
    ingest.edit_track(path, "New title", ["A"], "")
    ingest.edit_track(path, "Newer title", ["A"], "")

    now = time.time()
    assert navidrome.scan_if_due(now) is False  # still within the quiet minute
    assert nd == []
    assert navidrome.scan_if_due(now + navidrome.FULL_SCAN_QUIET_SECONDS + 1) is True
    assert len(nd) == 1
    assert navidrome.scan_if_due(now + 3600) is False  # nothing new asked for
    assert len(nd) == 1


def test_cover_and_loudness_backfills_ask_for_a_full_scan(nd, monkeypatch):
    path = _track()
    monkeypatch.setattr(
        ingest, "get_hd_cover", lambda a, t: (b"\xff\xd8\xff\xe0" + b"0" * 32, "jpg")
    )
    assert fix_covers.backfill(config.LIBRARY, delay=0, sleep=lambda s: None) == (1, 0)
    assert navidrome.scan_if_due(time.time() + 3600) is True

    assert loudness.backfill(config.LIBRARY, Path("/nonexistent.db")) == (1, 0, 0)
    assert navidrome.scan_if_due(time.time() + 3600) is True
    assert len(nd) == 2
    assert path.exists()


def test_nothing_is_asked_of_navidrome_when_it_is_not_configured(monkeypatch):
    db.db_init()
    monkeypatch.setattr(navidrome, "full_scan", lambda: pytest.fail("Navidrome was called"))
    ingest.edit_track(_track(), "T", ["A"], "")
    monkeypatch.setattr(config, "NAVIDROME_USER", "u")
    monkeypatch.setattr(config, "NAVIDROME_PASSWORD", "p")
    assert navidrome.scan_if_due(time.time() + 3600) is False


def test_a_scan_that_failed_is_asked_again(nd, monkeypatch):
    navidrome.request_full_scan()

    def down():
        raise navidrome.NavidromeUnavailable("down")

    monkeypatch.setattr(navidrome, "full_scan", down)
    later = time.time() + 3600
    assert navidrome.scan_if_due(later) is False
    monkeypatch.setattr(navidrome, "full_scan", lambda: nd.append(0.0))
    assert navidrome.scan_if_due(later) is True
    assert len(nd) == 1


def test_the_sync_loop_starts_the_scan(nd, monkeypatch):
    navidrome.request_full_scan()
    monkeypatch.setattr(navidrome, "FULL_SCAN_QUIET_SECONDS", 0)
    monkeypatch.setattr(sync, "check", lambda apply: {})
    waits = iter([False, True])
    monkeypatch.setattr(sync._stop, "wait", lambda seconds: next(waits))
    sync._loop()
    assert len(nd) == 1
