"""ffmpeg/ffprobe and Deno: detected at startup and reported by /health."""

from adder import ingest

# Captured at import: conftest replaces ingest.check_dependencies for every test.
real_check = ingest.check_dependencies


def test_check_reads_path(monkeypatch):
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None if name == "deno" else "/x")
    assert real_check() == {"ffmpeg": "ok", "js_runtime": "missing"}

    monkeypatch.setattr(ingest.shutil, "which", lambda name: None if name == "ffprobe" else "/x")
    assert real_check() == {"ffmpeg": "missing", "js_runtime": "ok"}
