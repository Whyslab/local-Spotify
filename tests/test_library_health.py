"""The library health report and its two automatic fixes."""

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4, MP4Cover

from adder import config, fix_covers, library, library_health, loudness, runtime

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
AUTH = {"Authorization": "Bearer test-secret"}


def track(root, rel, title, album="", cover=False, gain=None, number=None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, path)
    audio = MP4(path)
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = ["A"]
    if album:
        audio["\xa9alb"] = [album]
    if number:
        audio["trkn"] = [(number, 0)]
    if cover:
        audio["covr"] = [MP4Cover(b"\xff\xd8\xff" + b"0" * 16, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    if gain is not None:
        loudness.write(path, gain, 0.9)
    return path


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    root = tmp_path / "library"
    monkeypatch.setattr(config, "LIBRARY", root)
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(library, "_FILE_ROWS", {})
    monkeypatch.setattr(library_health, "_job", dict(library_health._job, running=False))
    track(root, "A/good.m4a", "Good", album="Real Album", cover=True, gain=-3.0, number=4)
    track(root, "A/bare.m4a", "Bare")
    track(root, "A/single.m4a", "Single", album="Single", cover=True, gain=-1.0)
    (root / "A" / "broken.m4a").write_bytes(b"not audio")
    library.invalidate_library_index()
    return root


def test_the_report_counts_each_problem(lib):
    report = library_health.report()
    problems = report["problems"]

    assert report["tracks"] == 3
    assert problems["no_cover"] == {"count": 1, "examples": ["A/bare.m4a"]}
    assert problems["no_loudness"]["examples"] == ["A/bare.m4a"]
    assert problems["no_album"]["examples"] == ["A/bare.m4a"]
    assert problems["fallback_single"]["examples"] == ["A/single.m4a"]
    assert problems["unreadable"]["examples"] == ["A/broken.m4a"]


def _wait(timeout=10):
    deadline = time.time() + timeout
    while library_health.report()["job"]["running"] and time.time() < deadline:
        time.sleep(0.05)
    job = library_health.report()["job"]
    assert not job["running"], "the background fix did not finish in time"
    return job


def test_the_loudness_fix_measures_what_is_missing(lib):
    library_health.start_fix("loudness")
    job = _wait()

    assert job["result"] == {"measured": 1, "failed": 1}  # the broken file cannot be measured
    assert library_health.report()["problems"]["no_loudness"]["count"] == 0


def test_the_cover_fix_uses_the_service_lookup(lib, monkeypatch):
    monkeypatch.setattr(fix_covers, "find_cover", lambda a, t: (b"\xff\xd8\xff" + b"1" * 16, "jpg"))

    library_health.start_fix("covers")
    job = _wait()

    assert job["result"] == {"added": 1, "not_found": 0}
    assert library_health.report()["problems"]["no_cover"]["count"] == 0


def test_one_fix_at_a_time(lib, monkeypatch):
    monkeypatch.setattr(
        library_health, "_job", dict(library_health._job, running=True, what="covers")
    )

    with pytest.raises(RuntimeError):
        library_health.start_fix("loudness")


def test_the_api(lib, monkeypatch):
    from adder import app as app_module

    monkeypatch.setattr(library_health, "start_fix", lambda what: {"what": what, "running": True})
    with TestClient(app_module.app) as client:
        assert client.get("/api/library/health").status_code == 401
        assert client.get("/api/library/health", headers=AUTH).json()["tracks"] == 3
        started = client.post("/api/library/health/fix", json={"what": "covers"}, headers=AUTH)
        assert started.json() == {"what": "covers", "running": True}
