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
import json
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from adder import config  # noqa: E402
from adder.loudness import backfill, file_hash  # noqa: E402,F401  (file_hash: used by tests)

logger = logging.getLogger("replaygain")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO / "adder" / "adder.db")
    parser.add_argument("--limit", type=int, default=None)
    # Files that could not be measured, by size and mtime (see backfill).
    parser.add_argument(
        "--failures", type=Path, default=REPO / "adder" / "replaygain-failures.json"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    root = args.library or config.LIBRARY
    failures = load_failures(args.failures)
    measured, tagged, failed = backfill(root, args.db, args.limit, failures=failures)
    # Files that are gone are not failures any more.
    failures = {path: stamp for path, stamp in failures.items() if (root / path).is_file()}
    save_failures(args.failures, failures)
    logger.info(
        "Measured %d, already tagged %d, failed %d (%d known failures in all, skipped)",
        measured,
        tagged,
        failed,
        len(failures),
    )
    # Non-zero on new failures: the nightly unit then shows as failed and the
    # failure notice fires, instead of a "success" that measured nothing. A
    # file that failed before and has not changed does not fire it again.
    return 1 if failed else 0


def load_failures(path: Path) -> dict[str, list[int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_failures(path: Path, failures: dict[str, list[int]]) -> None:
    try:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(failures, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        logger.warning("Could not remember the failures in %s: %s", path, exc)


if __name__ == "__main__":
    sys.exit(main())
