"""Массовая миграция плейлиста: spotify_tracks_youtube.csv -> Normalized Library.

Запуск:
    source ../adder/.venv/bin/activate
    python migrate_playlist.py [путь/к/spotify_tracks_youtube.csv]

Идемпотентен: существующие треки пропускает (SKIP), безопасно перезапускать.
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from mutagen.mp4 import MP4, MP4Cover

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent / "adder"))
from config import LIBRARY, DELAY_BETWEEN_TRACKS

CSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("spotify_tracks_youtube.csv")
TMP_DIR = Path(__file__).resolve().parent / "tmp"
DELAY = DELAY_BETWEEN_TRACKS  # пауза между треками (rate-limit iTunes)


def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    name = re.sub(r"[\[\]'\"]", "", str(name))
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def itunes_cover(artist: str, title: str):
    q = f"{artist} {title}".replace(" ", "+")
    for _ in range(3):
        try:
            r = requests.get(f"https://itunes.apple.com/search?term={q}&limit=1&entity=song", timeout=10)
        except Exception:
            time.sleep(5)
            continue
        if r.status_code in (403, 429):
            time.sleep(30)
            continue
        if r.ok and r.json().get("resultCount", 0) > 0:
            art = r.json()["results"][0].get("artworkUrl100", "").replace("100x100bb", "3000x3000bb")
            img = requests.get(art, timeout=15)
            if img.ok:
                return img.content, "jpg"
        return None
    return None


def fetch_cover(artist, title, thumb_url):
    cover = itunes_cover(artist, title)
    if cover:
        return cover
    if thumb_url:
        try:
            img = requests.get(thumb_url, timeout=15)
            if img.ok:
                return img.content, ("png" if img.content.startswith(b"\x89PNG") else "jpg")
        except Exception:
            pass
    return None, None


def unique_path(base: Path) -> Path:
    p, n = base, 1
    while p.exists():
        p = base.with_name(f"{base.stem} ({n}){base.suffix}")
        n += 1
    return p


def download(url: str):
    p = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "m4a", "--audio-quality", "0",
         "--no-playlist", "--print-json", "-q", "-o", str(TMP_DIR / "%(id)s.%(ext)s"), url],
        capture_output=True, text=True, timeout=600,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    info = json.loads(p.stdout.strip().splitlines()[-1])
    f = TMP_DIR / f"{info['id']}.m4a"
    if not f.exists():
        found = list(TMP_DIR.glob(f"{info['id']}.*"))
        if not found:
            raise RuntimeError("аудиофайл не найден после скачивания")
        f = found[0]
    return f, info


def main():
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("youtube_url") or "").strip()]

    print(f"Треков с YouTube-ссылками: {len(rows)}")
    ok = skip = fail = 0
    for i, row in enumerate(rows, 1):
        artist = sanitize_filename((row.get("artists") or "Unknown").split(",")[0])
        title = sanitize_filename(row.get("name") or "Unknown")
        base = LIBRARY / artist / "Singles" / f"{title}.m4a"

        if base.exists():
            skip += 1
            print(f"[{i}/{len(rows)}] SKIP {artist} - {title}")
            continue

        try:
            file, info = download(row["youtube_url"].strip())
            cover, fmt = fetch_cover(artist, title, info.get("thumbnail"))

            target = unique_path(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file), str(target))

            audio = MP4(target)
            audio["\xa9nam"] = title
            audio["\xa9ART"] = artist
            audio["aART"] = artist
            audio["\xa9alb"] = "Singles"
            if cover:
                audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG if fmt == "png" else MP4Cover.FORMAT_JPEG)]
            audio.save()
            ok += 1
            print(f"[{i}/{len(rows)}] OK   {artist} - {title}")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(rows)}] FAIL {artist} - {title}: {str(e)[:200]}")
        time.sleep(DELAY)

    print(f"\nГотово: ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    main()
