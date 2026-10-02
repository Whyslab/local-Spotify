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


def test_the_backfill_stops_when_the_service_does(tmp_path, monkeypatch):
    """sleep — это shutdown_event.wait: True значит «служба останавливается»."""
    for artist in ("A", "B", "C"):
        track(tmp_path, artist, "Song")
    asked = []
    monkeypatch.setattr(fix_covers, "find_cover", lambda artist, title: asked.append(artist))

    fix_covers.backfill(tmp_path, delay=1.0, sleep=lambda seconds: True)

    assert len(asked) == 1


def test_a_new_cover_keeps_the_arrival_date(tmp_path, monkeypatch):
    import os

    path = track(tmp_path, "A", "Old")
    os.utime(path, (1_600_000_000, 1_600_000_000))
    monkeypatch.setattr(fix_covers, "find_cover", lambda artist, title: (JPEG, "jpg"))

    assert fix_covers.backfill(tmp_path, delay=0, sleep=lambda seconds: None) == (1, 0)
    assert path.stat().st_mtime == 1_600_000_000


def _webp(tmp_path: Path) -> bytes:
    import subprocess

    out = tmp_path / "thumb.webp"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x36", "-frames:v", "1",
         str(out)],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


def _audio_md5(path: Path) -> str:
    import subprocess

    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _with_webp_cover(path: Path, webp: bytes) -> None:
    from mutagen.mp4 import MP4Cover

    audio = MP4(path)
    # What the service did before 2026-09-24: a YouTube WebP labelled JPEG.
    audio["covr"] = [MP4Cover(webp, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def test_a_webp_labelled_as_jpeg_is_replaced_by_a_real_cover(tmp_path, monkeypatch):
    path = track(tmp_path / "lib", "A", "Song")
    _with_webp_cover(path, _webp(tmp_path))
    monkeypatch.setattr(ingest, "get_hd_cover", lambda artist, title: (JPEG, "jpg"))

    added, missing = fix_covers.backfill(tmp_path / "lib", delay=0, sleep=lambda s: False)

    assert (added, missing) == (1, 0)
    assert bytes(MP4(path)["covr"][0]) == JPEG


def test_a_webp_cover_nothing_better_for_is_converted_to_jpeg(tmp_path, monkeypatch):
    path = track(tmp_path / "lib", "A", "Song")
    _with_webp_cover(path, _webp(tmp_path))
    before = _audio_md5(path)
    monkeypatch.setattr(ingest, "get_hd_cover", lambda artist, title: (None, None))
    monkeypatch.setattr(enrich, "lookup", lambda artist, title: None)

    added, missing = fix_covers.backfill(tmp_path / "lib", delay=0, sleep=lambda s: False)

    assert (added, missing) == (1, 0)
    cover = bytes(MP4(path)["covr"][0])
    assert ingest.image_format(cover) == "jpg"
    assert _audio_md5(path) == before, "the audio itself is never touched"
