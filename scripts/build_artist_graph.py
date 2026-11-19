#!/usr/bin/env python3
"""Ночной проход графа артистов (adder/artist_graph.py): ListenBrainz и Deezer.

    .venv/bin/python scripts/build_artist_graph.py [--budget 30]

Спрашивает только тех, о ком не спрашивал месяц (промахи — через неделю),
и сохраняется после каждого артиста: обрыв ничего не теряет.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=float, default=30, help="minutes")
    args = parser.parse_args()
    os.environ.setdefault("API_TOKEN", "artist-graph")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from adder import artist_graph, library

    result = artist_graph.refresh(library.library_index(), budget=args.budget * 60)
    print(result, artist_graph.describe(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
