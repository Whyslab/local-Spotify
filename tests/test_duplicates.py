from pathlib import Path

import pytest


@pytest.fixture()
def duplicate_module(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-secret")

    import importlib
    import sys

    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    app_module = importlib.import_module("adder.app")

    monkeypatch.setattr(app_module, "API_TOKEN", "test-secret")
    monkeypatch.setattr(app_module, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(app_module, "TMP_DIR", tmp_path / "tmp")

    app_module.LIBRARY.mkdir(parents=True)
    app_module.TMP_DIR.mkdir(parents=True)

    return app_module


def test_file_sha256_matches_identical_files(duplicate_module, tmp_path):
    app = duplicate_module

    first = tmp_path / "first.m4a"
    second = tmp_path / "second.m4a"

    payload = b"same audio content"
    first.write_bytes(payload)
    second.write_bytes(payload)

    assert app.file_sha256(first) == app.file_sha256(second)


def test_file_sha256_differs_for_different_files(duplicate_module, tmp_path):
    app = duplicate_module

    first = tmp_path / "first.m4a"
    second = tmp_path / "second.m4a"

    first.write_bytes(b"audio A")
    second.write_bytes(b"audio B")

    assert app.file_sha256(first) != app.file_sha256(second)


def test_identical_library_content_is_detected(duplicate_module):
    app = duplicate_module

    existing = app.LIBRARY / "Mozart" / "Singles" / "Lacrimosa.m4a"
    incoming = app.TMP_DIR / "incoming.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"identical audio bytes")
    incoming.write_bytes(b"identical audio bytes")

    duplicate = app.find_duplicate_library_file(incoming)

    assert duplicate == existing


def test_different_content_is_not_detected_as_duplicate(duplicate_module):
    app = duplicate_module

    existing = app.LIBRARY / "Mozart" / "Singles" / "Lacrimosa.m4a"
    incoming = app.TMP_DIR / "incoming.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"audio A")
    incoming.write_bytes(b"audio B")

    assert app.find_duplicate_library_file(incoming) is None


def test_same_filename_with_different_content_gets_unique_path(duplicate_module):
    app = duplicate_module

    target_dir = app.LIBRARY / "Mozart" / "Singles"
    target_dir.mkdir(parents=True)

    base = target_dir / "Lacrimosa.m4a"
    base.write_bytes(b"existing audio")

    incoming = target_dir / "Lacrimosa.m4a"
    assert app.unique_path(incoming) == target_dir / "Lacrimosa (1).m4a"


def test_identical_content_with_different_filename_is_duplicate(duplicate_module):
    app = duplicate_module

    existing = app.LIBRARY / "Mozart" / "Singles" / "Lacrimosa.m4a"
    incoming = app.TMP_DIR / "completely-different-name.m4a"

    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"same exact file")
    incoming.write_bytes(b"same exact file")

    duplicate = app.find_duplicate_library_file(incoming)

    assert duplicate == existing
    assert duplicate.name == "Lacrimosa.m4a"
