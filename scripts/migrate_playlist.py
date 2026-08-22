"""Массовая миграция Spotify CSV -> Normalized Library.

Файлы сначала обрабатываются в TMP_DIR. В Library они попадают только после
успешной metadata/artwork/M4A validation. Это делает миграцию безопасной при
ошибках и повторном запуске.
"""
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from mutagen.mp4 import MP4, MP4Cover

sys.path.insert(0, str(Path(__file__).parent.parent / "adder"))
from config import LIBRARY, DELAY_BETWEEN_TRACKS, MIN_FREE_SPACE_MB

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

CSV_PATH = (
    Path(sys.argv[1]).expanduser().resolve()
    if len(sys.argv) > 1
    else PROJECT_ROOT / "spotify_tracks_youtube.csv"
)

TMP_DIR = SCRIPT_DIR / "tmp"
DELAY = DELAY_BETWEEN_TRACKS


def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    name = re.sub(r"[\[\]'\"]", "", str(name))
    return re.sub(r'[\\/*?:"<>|]', "", name).strip() or "Unknown"


def validate_m4a(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError("Downloaded M4A is missing or empty")
    audio = MP4(path)
    if not audio.info or not getattr(audio.info, "length", 0) > 0:
        raise RuntimeError("Downloaded M4A has no valid audio stream")


def itunes_cover(artist: str, title: str):
    params = {"term": f"{artist} {title}", "limit": 1, "entity": "song"}
    for _ in range(3):
        try:
            r = requests.get(
                "https://itunes.apple.com/search",
                params=params,
                timeout=10,
            )
            if r.status_code in (403, 429):
                time.sleep(30)
                continue
            if r.ok and r.json().get("resultCount", 0) > 0:
                art = r.json()["results"][0].get("artworkUrl100", "").replace(
                    "100x100bb", "3000x3000bb"
                )
                img = requests.get(art, timeout=15)
                if img.ok:
                    return img.content, "jpg"
        except Exception:
            time.sleep(5)
    return None, None


def fetch_cover(artist: str, title: str, thumb_url: str | None):
    cover = itunes_cover(artist, title)
    if cover[0] is not None:
        return cover
    if thumb_url:
        try:
            img = requests.get(thumb_url, timeout=15)
            if img.ok:
                fmt = "png" if img.content.startswith(b"\x89PNG") else "jpg"
                return img.content, fmt
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
        [
            sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "m4a",
            "--audio-quality", "0", "--no-playlist", "--print-json", "-q",
            "-o", str(TMP_DIR / "%(id)s.%(ext)s"), url,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    info = json.loads(p.stdout.strip().splitlines()[-1])
    file = TMP_DIR / f"{info['id']}.m4a"
    if not file.exists():
        found = list(TMP_DIR.glob(f"{info['id']}.*"))
        if not found:
            raise RuntimeError("Аудиофайл не найден после скачивания")
        file = found[0]
    return file, info


def main():
    if not CSV_PATH.is_file():
        raise SystemExit(f"CSV input not found: {CSV_PATH}")

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

        temp_file = None
        try:
            free_mb = shutil.disk_usage(TMP_DIR).free // (1024 * 1024)
            if free_mb < MIN_FREE_SPACE_MB:
                raise RuntimeError(
                    f"Insufficient disk space: {free_mb}MB free, {MIN_FREE_SPACE_MB}MB required"
                )

            temp_file, info = download(row["youtube_url"].strip())
            validate_m4a(temp_file)

            cover, fmt = fetch_cover(artist, title, info.get("thumbnail"))

            # Metadata is written while the file is still outside Library.
            audio = MP4(temp_file)
            audio["\xa9nam"] = title
            audio["\xa9ART"] = artist
            audio["aART"] = artist
            audio["\xa9alb"] = "Singles"
            if cover:
                audio["covr"] = [
                    MP4Cover(
                        cover,
                        imageformat=(MP4Cover.FORMAT_PNG if fmt == "png" else MP4Cover.FORMAT_JPEG),
                    )
                ]
            audio.save()

            # Verify the processed file before publication.
            verify = MP4(temp_file)
            if not verify.get("\xa9nam"):
                raise RuntimeError("Metadata verification failed")
            validate_m4a(temp_file)

            # Only now publish atomically to the final Library path.
            target = unique_path(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_file.replace(target)
            temp_file = None

            ok += 1
            print(f"[{i}/{len(rows)}] OK   {artist} - {title}")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(rows)}] FAIL {artist} - {title}: {str(e)[:200]}")
        finally:
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass
        time.sleep(DELAY)

    print(f"\nГотово: ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    main()
