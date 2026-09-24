#!/usr/bin/env python3
"""Add ReplayGain tags to library tracks that do not have them yet.

New downloads are measured as they arrive (adder/loudness.py); this catches up
on everything that was in the library before, and runs each night ahead of the
audio analysis, so later additions by other means are covered too.

Two things a retag must not disturb:

* The file's modification time. The library treats it as "when this track
  arrived here", so rewriting a thousand files would make all of them "new".
* The audio analysis. scripts/analyze_audio.py keys every measured track by
  the SHA-256 of the whole file; a new tag changes that hash, and the next pass
  would spend an hour re-measuring music that did not change. Rows whose hash
  matched the file before tagging are moved to the new hash.

    .venv/bin/python scripts/replaygain.py            # the whole library
    .venv/bin/python scripts/replaygain.py --limit 50 # a first taste
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from adder import config, library, loudness  # noqa: E402

logger = logging.getLogger("replaygain")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _analysis(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path, timeout=30)
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audio_features'"
    ).fetchone()
    if not exists:
        con.close()
        return None
    return con


def backfill(root: Path, db_path: Path, limit: int | None = None) -> tuple[int, int, int]:
    """Tag what is missing. Returns (measured, already tagged, failed)."""
    root = root.resolve()
    analysis = _analysis(db_path)
    measured = tagged = failed = 0
    files = sorted(
        p for suffix in library.AUDIO_SUFFIXES for p in root.rglob(f"*{suffix}") if p.is_file()
    )
    try:
        for path in files:
            if limit is not None and measured >= limit:
                break
            try:
                if library.read_tags(path).get("gain") is not None:
                    tagged += 1
                    continue
            except Exception:
                failed += 1
                continue

            stat = path.stat()
            before = file_hash(path) if analysis else None
            try:
                gain = loudness.apply(path)
            except Exception as exc:
                logger.warning("%s: %s", path.relative_to(root), exc)
                gain = None
            finally:
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if gain is None:
                failed += 1
                continue
            measured += 1
            if analysis is not None:
                analysis.execute(
                    "UPDATE audio_features SET sha256 = ? WHERE path = ? AND sha256 = ?",
                    (file_hash(path), str(path.relative_to(root)), before),
                )
                analysis.commit()
            logger.info("%+.2f dB  %s", gain, path.relative_to(root))
    finally:
        if analysis is not None:
            analysis.close()
    library.invalidate_library_index()
    return measured, tagged, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO / "adder" / "adder.db")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    measured, tagged, failed = backfill(args.library or config.LIBRARY, args.db, args.limit)
    logger.info("Measured %d, already tagged %d, failed %d", measured, tagged, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
