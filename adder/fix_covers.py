"""Добивает обложки для всех треков без covr-тега. iTunes (throttled) -> Deezer fallback."""

import sys
import time
from pathlib import Path

import requests
from mutagen.mp4 import MP4, MP4Cover

# Explicitly add this file's own directory to sys.path so "import config"
# resolves whether this script is run directly (python adder/fix_covers.py,
# where sys.path[0] already happens to be this directory) or as a module
# (python -m adder.fix_covers, where it does not). Matches the pattern
# already used by scripts/*.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import unified configuration
from config import DELAY_BETWEEN_TRACKS, LIBRARY

DELAY = DELAY_BETWEEN_TRACKS  # защита от rate-limit iTunes


def itunes_cover(artist: str, title: str):
    params = {"term": f"{artist} {title}", "limit": 1, "entity": "song"}
    for _ in range(3):
        try:
            r = requests.get("https://itunes.apple.com/search", params=params, timeout=10)
        except Exception:
            time.sleep(5)
            continue
        if r.status_code in (403, 429):  # rate-limit: ждём и повторяем
            time.sleep(30)
            continue
        if r.ok and r.json().get("resultCount", 0) > 0:
            art = (
                r.json()["results"][0].get("artworkUrl100", "").replace("100x100bb", "3000x3000bb")
            )
            img = requests.get(art, timeout=15)
            if img.ok:
                return img.content, "jpg"
        return None  # найдено не было — ретраи бессмысленны
    return None


def deezer_cover(artist: str, title: str):
    try:
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": f"{artist} {title}", "limit": 1},
            timeout=10,
        )
        if r.ok and r.json().get("data"):
            url = r.json()["data"][0].get("album", {}).get("cover_xl")
            if url:
                img = requests.get(url, timeout=15)
                if img.ok:
                    return img.content, "jpg"
    except Exception:
        pass
    return None


def main():
    files = sorted(LIBRARY.rglob("*.m4a"))
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

        cover = itunes_cover(artist, title)
        if not cover:
            cover = deezer_cover(artist, title)
            time.sleep(0.4)

        if cover:
            data, fmt = cover
            fmt_c = MP4Cover.FORMAT_PNG if fmt == "png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(data, imageformat=fmt_c)]
            audio.save()
            ok += 1
            print(f"[{i}/{len(missing)}] OK   {artist} - {title}")
        else:
            miss += 1
            print(f"[{i}/{len(missing)}] MISS {artist} - {title}")
        time.sleep(DELAY)

    print(f"\nГотово: обложек добавлено {ok}, не найдено {miss}")


if __name__ == "__main__":
    main()
