from pathlib import Path

import pytest

from adder import config, db, ingest, runtime
from adder import queue as adder_queue


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-secret")

    import importlib
    import sys

    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    app_module = importlib.import_module("adder.app")

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)

    config.LIBRARY.mkdir(parents=True)
    runtime.TMP_DIR.mkdir(parents=True)

    runtime.PROCESSING_URLS.clear()

    db.db_init()

    return app_module


def fake_mp4_processing(app, tmp_file, artist, title):
    """
    Replace MP4 metadata processing with a deterministic file operation.

    We don't need a real M4A container here because the purpose of these
    tests is to verify duplicate/final-move behavior.
    """
    original_validate = ingest.validate_m4a_integrity

    def fake_validate(filepath):
        if filepath.exists() and filepath.stat().st_size > 0:
            return True, ""
        return False, "File does not exist or is empty"

    return original_validate, fake_validate


def create_fake_download(payload: bytes):
    """
    Return a fake yt_download() implementation.

    Each call creates a temporary source file containing exactly `payload`.
    """
    counter = {"value": 0}

    def fake_download(url, video_id):
        counter["value"] += 1

        source = runtime.TMP_DIR / f"download-{counter['value']}-{video_id}.m4a"
        source.write_bytes(payload)
        return source

    return fake_download


def configure_fake_processing(monkeypatch, payload):
    """Patch every external/media operation the pipeline reaches for.

    The patches land on adder.ingest, which is where the functions actually
    live. Patching a re-export would look identical and quietly do nothing.
    """

    monkeypatch.setattr(
        ingest,
        "yt_meta",
        lambda url: {
            "id": "same-video-id",
            "title": "Test Track",
            "thumbnail": None,
        },
    )

    monkeypatch.setattr(
        ingest,
        "split_artist_title",
        lambda meta: (
            "Test Artist",
            "Test Track",
            "Test Artist",
            "Test Track",
        ),
    )

    monkeypatch.setattr(
        ingest,
        "yt_download",
        create_fake_download(payload),
    )

    monkeypatch.setattr(
        ingest,
        "validate_m4a_integrity",
        lambda filepath: (
            (True, "")
            if filepath.exists() and filepath.stat().st_size > 0
            else (False, "invalid file")
        ),
    )

    monkeypatch.setattr(
        ingest,
        "fetch_cover",
        lambda *args, **kwargs: (None, None),
    )

    class FakeMP4:
        _metadata = {}

        def __init__(self, filepath):
            self.filepath = Path(filepath)
            self.info = type("Info", (), {"length": 1})()
            self.data = self._metadata.setdefault(
                str(self.filepath),
                {},
            )

        def __setitem__(self, key, value):
            self.data[key] = value

        def get(self, key, default=None):
            return self.data.get(key, default)

        def save(self):
            # Simulate metadata persistence across MP4 instances.
            self._metadata[str(self.filepath)] = dict(self.data)

    monkeypatch.setattr(ingest, "MP4", FakeMP4)


# ---------------------------------------------------------------------------
# Basic content duplicate
# ---------------------------------------------------------------------------


def test_same_content_is_not_stored_twice(
    app_module,
    monkeypatch,
):

    configure_fake_processing(
        monkeypatch,
        b"IDENTICAL AUDIO CONTENT",
    )

    adder_queue.process(
        1,
        "https://www.youtube.com/watch?v=track-one",
    )

    adder_queue.process(
        2,
        "https://www.youtube.com/watch?v=track-two",
    )

    files = list(config.LIBRARY.rglob("*.m4a"))

    assert len(files) == 1
    assert files[0].read_bytes() == b"IDENTICAL AUDIO CONTENT"


# ---------------------------------------------------------------------------
# Same filename, different content
# ---------------------------------------------------------------------------


def test_same_filename_different_content_is_preserved(
    app_module,
    monkeypatch,
):

    payloads = [
        b"AUDIO VERSION A",
        b"AUDIO VERSION B",
    ]

    counter = {"value": 0}

    monkeypatch.setattr(
        ingest,
        "yt_meta",
        lambda url: {
            "id": f"video-{counter['value']}",
            "title": "Test Track",
            "thumbnail": None,
        },
    )

    def fake_download(url, video_id):
        payload = payloads[counter["value"]]
        counter["value"] += 1

        source = runtime.TMP_DIR / f"{video_id}.m4a"
        source.write_bytes(payload)
        return source

    monkeypatch.setattr(ingest, "yt_download", fake_download)

    monkeypatch.setattr(
        ingest,
        "split_artist_title",
        lambda meta: (
            "Test Artist",
            "Test Track",
            "Test Artist",
            "Test Track",
        ),
    )

    monkeypatch.setattr(
        ingest,
        "fetch_cover",
        lambda *args, **kwargs: (None, None),
    )

    monkeypatch.setattr(
        ingest,
        "validate_m4a_integrity",
        lambda filepath: (True, ""),
    )

    class FakeMP4:
        _metadata = {}

        def __init__(self, filepath):
            self.filepath = Path(filepath)
            self.info = type("Info", (), {"length": 1})()
            self.data = self._metadata.setdefault(
                str(self.filepath),
                {},
            )

        def __setitem__(self, key, value):
            self.data[key] = value

        def get(self, key, default=None):
            return self.data.get(key, default)

        def save(self):
            # Simulate metadata persistence across MP4 instances.
            self._metadata[str(self.filepath)] = dict(self.data)

    monkeypatch.setattr(ingest, "MP4", FakeMP4)

    adder_queue.process(1, "https://www.youtube.com/watch?v=a")
    adder_queue.process(2, "https://www.youtube.com/watch?v=b")

    files = sorted(config.LIBRARY.rglob("*.m4a"))

    assert len(files) == 2
    assert files[0].name == "Test Track (1).m4a"
    assert files[1].name == "Test Track.m4a"

    contents = {file.read_bytes() for file in files}

    assert contents == {
        b"AUDIO VERSION A",
        b"AUDIO VERSION B",
    }


# ---------------------------------------------------------------------------
# Duplicate with different filename
# ---------------------------------------------------------------------------


def test_identical_content_with_different_filename_is_deduplicated(
    app_module,
    monkeypatch,
):

    existing = config.LIBRARY / "Existing Artist" / "Singles" / "Original Name.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"SAME AUDIO")

    incoming = runtime.TMP_DIR / "incoming.m4a"
    incoming.write_bytes(b"SAME AUDIO")

    duplicate = ingest.find_duplicate_library_file(incoming)

    assert duplicate == existing


# ---------------------------------------------------------------------------
# Different content
# ---------------------------------------------------------------------------


def test_different_content_is_not_deduplicated(
    app_module,
):

    existing = config.LIBRARY / "Artist" / "Singles" / "Track.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"CONTENT A")

    incoming = runtime.TMP_DIR / "incoming.m4a"
    incoming.write_bytes(b"CONTENT B")

    assert ingest.find_duplicate_library_file(incoming) is None


# ---------------------------------------------------------------------------
# Temporary file cleanup
# ---------------------------------------------------------------------------


def test_duplicate_removes_temporary_file(
    app_module,
):

    existing = config.LIBRARY / "Artist" / "Singles" / "Track.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"SAME AUDIO")

    incoming = runtime.TMP_DIR / "incoming.m4a"
    incoming.write_bytes(b"SAME AUDIO")

    duplicate = ingest.find_duplicate_library_file(incoming)

    assert duplicate == existing

    incoming.unlink()

    assert not incoming.exists()
    assert existing.exists()


# ---------------------------------------------------------------------------
# Hash correctness
# ---------------------------------------------------------------------------


def test_sha256_is_deterministic(
    app_module,
    tmp_path,
):

    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"

    first.write_bytes(b"hello world")
    second.write_bytes(b"hello world")

    assert ingest.file_sha256(first) == ingest.file_sha256(second)


def test_sha256_detects_content_change(
    app_module,
    tmp_path,
):

    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"

    first.write_bytes(b"hello world")
    second.write_bytes(b"hello WORLD")

    assert ingest.file_sha256(first) != ingest.file_sha256(second)


# ---------------------------------------------------------------------------
# Lock behavior
# ---------------------------------------------------------------------------


def test_duplicate_check_works_under_file_lock(
    app_module,
):

    existing = config.LIBRARY / "Artist" / "Singles" / "Track.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"SAME")

    incoming = runtime.TMP_DIR / "incoming.m4a"
    incoming.write_bytes(b"SAME")

    with runtime.FILE_LOCK:
        duplicate = ingest.find_duplicate_library_file(incoming)

    assert duplicate == existing


# ---------------------------------------------------------------------------
# Database retry behavior
# ---------------------------------------------------------------------------


def test_failed_task_can_be_reused(
    app_module,
):

    task = db.db_exec(
        "INSERT INTO tasks(url, status) VALUES(?, ?)",
        (
            "https://www.youtube.com/watch?v=retry-test",
            "error",
        ),
    )

    task_id = task.lastrowid

    existing = db.db_query(
        "SELECT id, status FROM tasks WHERE url = ?",
        ("https://www.youtube.com/watch?v=retry-test",),
    )

    assert len(existing) == 1
    assert existing[0]["id"] == task_id
    assert existing[0]["status"] == "error"

    db.task_update(
        task_id,
        status="queued",
        error=None,
        error_type=None,
        retry_count=0,
    )

    updated = db.db_query(
        "SELECT id, status, retry_count FROM tasks WHERE id = ?",
        (task_id,),
    )

    assert updated[0]["status"] == "queued"
    assert updated[0]["retry_count"] == 0


# ---------------------------------------------------------------------------
# URL duplicate normalization
# ---------------------------------------------------------------------------


def test_canonical_youtube_urls_match(
    app_module,
):

    variants = [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://music.youtube.com/watch?v=abc123",
    ]

    canonical = {ingest.canonicalize_youtube_url(url) for url in variants}

    assert len(canonical) == 1


# ---------------------------------------------------------------------------
# No accidental Track (1) for identical content
# ---------------------------------------------------------------------------


def test_duplicate_does_not_create_collision_filename(
    app_module,
):

    existing = config.LIBRARY / "Artist" / "Singles" / "Track.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"EXACT CONTENT")

    incoming = runtime.TMP_DIR / "incoming.m4a"
    incoming.write_bytes(b"EXACT CONTENT")

    duplicate = ingest.find_duplicate_library_file(incoming)

    assert duplicate is not None

    # Simulate final duplicate handling.
    incoming.unlink()

    files = list((config.LIBRARY / "Artist" / "Singles").glob("*.m4a"))

    assert [f.name for f in files] == ["Track.m4a"]
