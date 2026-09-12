"""The library module: the safety net around deletion, and what the index carries."""

from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from adder import config, library, runtime


@pytest.fixture()
def temp_library(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    config.LIBRARY.mkdir(parents=True)
    library.invalidate_library_index()
    return config.LIBRARY


def test_delete_refuses_to_touch_the_real_library(temp_library, monkeypatch):
    """The fixtures redirect the library; if that ever stops working, fail loudly.

    Without this the tests would keep passing while moving the user's actual
    music into trash/ -- silent data loss dressed up as a green run.
    """
    monkeypatch.setattr(config, "LIBRARY", Path.home() / "Music" / "Normalized Library")

    with pytest.raises(RuntimeError, match="Refusing to delete a track"):
        library.delete_track("whatever/Singles/track.m4a")


def test_delete_moves_the_file_to_trash(temp_library):
    track = temp_library / "Artist" / "Singles" / "Song.m4a"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")

    result = library.delete_track("Artist/Singles/Song.m4a")

    assert not track.exists()
    assert (runtime.TRASH_DIR / "Artist" / "Singles" / "Song.m4a").read_bytes() == b"audio"
    assert result["deleted"] == "Artist/Singles/Song.m4a"
    # Empty artist and album folders are not left behind.
    assert not (temp_library / "Artist").exists()


def test_delete_refuses_a_path_that_escapes_the_library(temp_library):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        library.delete_track("../../etc/passwd")

    assert excinfo.value.status_code in (400, 404)


def _write_m4a(path: Path) -> None:
    """A real, minimal .m4a so mutagen can read a duration back out of it."""
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "2",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def test_index_carries_duration(temp_library):
    """Duration is read once here so the playlist writer and search need not re-open files."""
    track = temp_library / "Artist" / "Singles" / "Song.m4a"
    _write_m4a(track)
    audio = MP4(track)
    audio["\xa9nam"] = ["Song"]
    audio["\xa9ART"] = ["Artist"]
    audio.save()

    library.invalidate_library_index()
    rows = library.library_index()

    assert len(rows) == 1
    assert rows[0]["title"] == "Song"
    assert rows[0]["duration"] == pytest.approx(2.0, abs=0.2)
