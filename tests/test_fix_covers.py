"""The offline cover backfill uses the service's own cover lookup."""

import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from adder import enrich, fix_covers, ingest

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def track(library: Path, artist: str, title: str) -> Path:
    path = library / artist / "Singles" / f"{title}.m4a"
    path.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, path)
    audio = MP4(path)
    audio["\xa9ART"] = [artist]
    audio["\xa9nam"] = [title]
    audio.save()
    return path


def test_missing_covers_are_filled_from_itunes_then_deezer(tmp_path, monkeypatch):
    first = track(tmp_path, "A", "From iTunes")
    second = track(tmp_path, "B", "From Deezer")
    third = track(tmp_path, "C", "Nowhere")

    monkeypatch.setattr(
        ingest,
        "get_hd_cover",
        lambda artist, title: (JPEG, "jpg") if title == "From iTunes" else (None, None),
    )
    monkeypatch.setattr(
        enrich,
        "lookup",
        lambda artist, title: (
            enrich.TrackInfo(album="X", cover_url="https://dz/x.jpg")
            if title == "From Deezer"
            else None
        ),
    )
    monkeypatch.setattr(ingest, "fetch_cover_url", lambda url: (JPEG, "jpg"))

    added, missed = fix_covers.backfill(tmp_path, delay=0, sleep=lambda s: None)

    assert (added, missed) == (2, 1)
    assert bytes(MP4(first).tags["covr"][0]) == JPEG
    assert bytes(MP4(second).tags["covr"][0]) == JPEG
    assert "covr" not in MP4(third).tags


def test_tracks_that_already_have_a_cover_are_left_alone(tmp_path, monkeypatch):
    path = track(tmp_path, "A", "Has One")
    audio = MP4(path)
    from mutagen.mp4 import MP4Cover

    audio["covr"] = [MP4Cover(b"\x89PNG\r\n\x1a\nold", imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()
    monkeypatch.setattr(ingest, "get_hd_cover", lambda *a: (_ for _ in ()).throw(AssertionError))

    assert fix_covers.backfill(tmp_path, delay=0, sleep=lambda s: None) == (0, 0)
