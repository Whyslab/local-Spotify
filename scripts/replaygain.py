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
import logging
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    measured, tagged, failed = backfill(args.library or config.LIBRARY, args.db, args.limit)
    logger.info("Measured %d, already tagged %d, failed %d", measured, tagged, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
