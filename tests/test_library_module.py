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


def test_rebuilding_the_index_rereads_only_changed_files(tmp_path, monkeypatch):
    import os
    import shutil

    from adder import config, library

    fixture = Path(__file__).parent / "fixtures" / "tone.m4a"
    monkeypatch.setattr(config, "LIBRARY", tmp_path)
    monkeypatch.setattr(library, "_FILE_ROWS", {})
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        shutil.copy(fixture, tmp_path / name / "t.m4a")

    reads = []
    real = library.read_tags
    monkeypatch.setattr(library, "read_tags", lambda f: reads.append(f.parent.name) or real(f))

    library.invalidate_library_index()
    assert len(library.library_index()) == 3
    assert sorted(reads) == ["a", "b", "c"]

    reads.clear()
    later = (tmp_path / "b" / "t.m4a").stat().st_mtime + 10
    os.utime(tmp_path / "b" / "t.m4a", (later, later))
    (tmp_path / "c" / "t.m4a").unlink()
    library.invalidate_library_index()

    assert [row["path"] for row in library.library_index()] == ["a/t.m4a", "b/t.m4a"]
    assert reads == ["b"]


def test_a_retag_that_keeps_the_date_is_still_noticed(tmp_path, monkeypatch):
    import os
    import shutil

    from mutagen.mp4 import MP4

    from adder import config, library

    fixture = Path(__file__).parent / "fixtures" / "tone.m4a"
    monkeypatch.setattr(config, "LIBRARY", tmp_path)
    monkeypatch.setattr(library, "_FILE_ROWS", {})
    path = tmp_path / "t.m4a"
    shutil.copy(fixture, path)
    library.invalidate_library_index()
    library.library_index()

    stat = path.stat()
    audio = MP4(path)
    audio["\xa9nam"] = ["Renamed"]
    audio.save()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # as the tag editor does
    library.invalidate_library_index()

    assert library.library_index()[0]["title"] == "Renamed"


def test_a_change_during_a_rebuild_is_not_hidden_behind_the_cache(tmp_path, monkeypatch):
    import shutil

    from mutagen.mp4 import MP4

    from adder import config, library

    fixture = Path(__file__).parent / "fixtures" / "tone.m4a"
    monkeypatch.setattr(config, "LIBRARY", tmp_path)
    monkeypatch.setattr(library, "_FILE_ROWS", {})
    path = tmp_path / "t.m4a"
    shutil.copy(fixture, path)

    real = library.read_tags

    def read_then_change(f):
        # The file changes, and the change is announced, while the rebuild is
        # still reading it - what a worker finishing a track does in real life.
        row = real(f)
        audio = MP4(f)
        audio["\xa9nam"] = ["Changed"]
        audio.save()
        library.invalidate_library_index()
        return row

    monkeypatch.setattr(library, "read_tags", read_then_change)
    library.invalidate_library_index()
    library.library_index()
    monkeypatch.setattr(library, "read_tags", real)

    assert library.library_index()[0]["title"] == "Changed"
