#!/usr/bin/env python3
"""Скачать модели Essentia для умного перемешивания по звуку в adder/models/.

Модели — под лицензией CC BY-NC-SA 4.0 (https://essentia.upf.edu/models.html):
для домашнего некоммерческого сервиса можно, класть в публичный git — нет.
Поэтому они не в репозитории, а качаются этим скриптом, и каждый файл
сверяется с хешем: подменённый или недокачанный файл не будет загружен.

    .venv/bin/python scripts/fetch_models.py

Уже скачанные файлы с верным хешем не качаются второй раз.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "adder" / "models"
BASE = "https://essentia.upf.edu/models"

# Путь на сервере → sha256 (сняты 19.11.2026).
FILES = {
    "feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx": (
        "a280825b334797cf677939db8cd5762c0392aedd0ca6415dbc1cd083f045e43c"
    ),
    "feature-extractors/musicnn/msd-musicnn-1.onnx": (
        "49668ffec47e52e94b96f45930bb46a28a1368d4bdfb5c05378fa834aca616e1"
    ),
    "classification-heads/deam/deam-msd-musicnn-2.onnx": (
        "2cb8f33f188d73fcb651f7c065b049959166c7852f454a59c13e66fe2c092bde"
    ),
    "classification-heads/mood_happy/mood_happy-discogs-effnet-1.onnx": (
        "0ca322819ef137b4b87e9866bffe7370a630e6f1165184ec106326cef6f81e06"
    ),
    "classification-heads/mood_sad/mood_sad-discogs-effnet-1.onnx": (
        "1a50d11c23181bdfdeabc7d6032a2dad829dfc19c3776d2ee57ef1724cb08805"
    ),
    "classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.onnx": (
        "de36550b5d1660791ad732ed6de6ebfdc3e65dcf50b928b2578ddf103dbfb400"
    ),
    "classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.onnx": (
        "8ba6515a1e5943a72b3b475e3a25fc7a2ff04142c3eaa6aa0716fca371efdfff"
    ),
    "classification-heads/mood_party/mood_party-discogs-effnet-1.onnx": (
        "c50ac2106ec2f209dd04ad48756582df0e3f3512235310d1a4a3fcc453745f04"
    ),
    "classification-heads/danceability/danceability-discogs-effnet-1.onnx": (
        "9ce9b8c44f1dd5df5ffc124e5d41d67acf254232c1b90c7e057e079ab7cead73"
    ),
    "classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.onnx": (
        "20155e4c439714b0c45c08644b73c8e12d9dccb173bd4ab9934bf1e5aee837ca"
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def fetch(remote: str, expected: str, target: Path = TARGET) -> bool:
    """True — файл на месте и с верным хешем (скачан сейчас или раньше)."""
    final = target / Path(remote).name
    if final.exists() and sha256(final) == expected:
        return True
    partial = final.with_suffix(final.suffix + ".part")
    url = f"{BASE}/{remote}"
    with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as handle:  # noqa: S310
        while block := response.read(1 << 20):
            handle.write(block)
    got = sha256(partial)
    if got != expected:
        partial.unlink()
        print(f"  ✗ {final.name}: хеш {got[:12]}… вместо {expected[:12]}…", file=sys.stderr)
        return False
    partial.replace(final)
    return True


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    bad = 0
    for remote, expected in FILES.items():
        try:
            ok = fetch(remote, expected)
        except OSError as exc:
            print(f"  ✗ {Path(remote).name}: {exc}", file=sys.stderr)
            ok = False
        print(f"  {'✓' if ok else '✗'} {Path(remote).name}")
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
