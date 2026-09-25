"""Добивает обложки для всех треков без covr-тега: iTunes -> Deezer.

Поиск обложки — тот же, что у службы при добавлении трека (ingest.get_hd_cover
и Deezer через enrich), а не своя копия: копия уже разошлась с оригиналом и не
проверяла ни совпадение песни, ни то, что пришла действительно картинка.

Запуск из корня репозитория:

    .venv/bin/python -m adder.fix_covers
"""

import os
import sys
import time
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover

# Run as "python adder/fix_covers.py", sys.path[0] is adder/ itself, where
# adder/queue.py would shadow the standard library's queue. Point it at the
# repository root instead, which is what "python -m adder.fix_covers" gets.
if not __package__:
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from adder import config, enrich, ingest  # noqa: E402


def find_cover(artist: str, title: str):
    """(bytes, fmt) or None: iTunes first, then the album cover Deezer knows."""
    data, fmt = ingest.get_hd_cover(artist, title)
    if data:
        return data, fmt
    info = enrich.lookup(artist, title)
    if info and info.cover_url:
        data, fmt = ingest.fetch_cover_url(info.cover_url)
        if data:
            return data, fmt
    return None


def backfill(library: Path, delay: float, sleep=time.sleep) -> tuple[int, int]:
    """Add covers to every M4A without one. Returns (added, not found)."""
    files = sorted(library.rglob("*.m4a"))
    missing = []
    for f in files:
        try:
            audio = MP4(f)
        except Exception:
            continue
        if not audio.get("covr"):
            missing.append((f, audio))

    print(f"Всего файлов: {len(files)} | без обложек: {len(missing)}")
    ok = miss = 0
    for i, (f, audio) in enumerate(missing, 1):
        artist = (audio.get("\xa9ART") or [f.parent.parent.name])[0]
        title = (audio.get("\xa9nam") or [f.stem])[0]

        cover = find_cover(artist, title)
        if cover:
            data, fmt = cover
            fmt_c = MP4Cover.FORMAT_PNG if fmt == "png" else MP4Cover.FORMAT_JPEG
            # Reloaded right before saving (a tool may have swapped the file
            # since the scan), and the mtime put back: the library reads it as
            # the track's "added" date, and a new cover is not a new track.
            fresh = MP4(f)
            stamp = f.stat()
            fresh["covr"] = [MP4Cover(data, imageformat=fmt_c)]
            fresh.save()
            os.utime(f, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            ok += 1
            print(f"[{i}/{len(missing)}] OK   {artist} - {title}")
        else:
            miss += 1
            print(f"[{i}/{len(missing)}] MISS {artist} - {title}")
        sleep(delay)  # защита от rate-limit iTunes

    print(f"\nГотово: обложек добавлено {ok}, не найдено {miss}")
    return ok, miss


def main():
    backfill(config.LIBRARY, config.DELAY_BETWEEN_TRACKS)


if __name__ == "__main__":
    main()
