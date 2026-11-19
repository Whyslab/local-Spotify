#!/usr/bin/env python3
"""Бэктест умного по звучанию на журнале прослушиваний (adder/sonic.py).

Вопрос, который задаёт исследование 19.11.2026 до внедрения: **растут ли
ранние пропуски с расстоянием перехода по звуку?** Если нет — эмбеддинг
для порядка не помогает, и веса надо менять.

Для каждой пары соседних прослушиваний одной сессии (пауза ≤ 30 мин) —
режим второго трека, расстояние по звуку (косинус EffNet), то же ли облако,
тот же ли язык, похожи ли артисты. Ранний пропуск — пропуск в первые 30 с.
Режимы — порознь (smart, plain, перемешанное в плеере, радио, слепое);
игра по порядку (manual без shuffled) не берётся: там порядок задал альбом,
а не перемешивание.

Дальше — грубая оценка «что было бы»: если пропуск зависит только от
расстояния перехода, какая доля пропусков ждёт очередь нового умного
(по её распределению расстояний) против старого и обычного. Это не
доказательство — финал за ухом (слепое сравнение).

    .venv/bin/python scripts/backtest_sonic.py [--db adder/adder.db]

База открывается только на чтение.
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SESSION_GAP = 30 * 60
EARLY = 30.0
BOOTSTRAP = 2000


def _mode(row: dict) -> str | None:
    mode = row["mode"] or "manual"
    if mode == "manual":
        return "shuffled" if row["shuffled"] else None
    return mode


def pairs(plays: list[dict]) -> list[tuple[dict, dict]]:
    out = []
    previous = None
    for play in plays:
        if previous is not None and play["at"] - previous["at"] <= SESSION_GAP:
            out.append((previous, play))
        previous = play
    return out


def early(play: dict) -> bool:
    return bool(play["skipped"]) and float(play["played_seconds"] or 0) < EARLY


def rate(items: list[bool]) -> float | None:
    return sum(items) / len(items) if items else None


def ci_difference(a: list[bool], b: list[bool], rng: random.Random) -> tuple[float, float]:
    """95 % интервал разницы долей a − b (бутстрэп)."""
    diffs = []
    for _ in range(BOOTSTRAP):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    return diffs[int(0.025 * BOOTSTRAP)], diffs[int(0.975 * BOOTSTRAP)]


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:4.1f} %"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO / "adder" / "adder.db")
    parser.add_argument("--lyrics", type=Path, default=None, help="папка кэша текстов")
    parser.add_argument("--graph", type=Path, default=None, help="artist-graph.json")
    parser.add_argument("--queues", type=int, default=40, help="сколько очередей на оценку")
    args = parser.parse_args()

    os.environ.setdefault("API_TOKEN", "backtest")
    import numpy as np

    from adder import artist_graph, library, lyrics, shuffle, sonic

    if args.lyrics:
        lyrics.CACHE_DIR = args.lyrics
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    index_rows = library.library_index()
    meta = {row["path"]: row for row in index_rows}
    sonic_rows = [dict(r) for r in con.execute("SELECT * FROM sonic").fetchall()]
    space = sonic.build_space(
        [r for r in sonic_rows if r["path"] in meta],
        sonic.languages(index_rows, lyrics.plain_text),
    )
    if space is None:
        print("звучания нет — сначала scripts/embed_audio.py")
        return 1
    graph = {}
    if args.graph and args.graph.exists():
        import json

        graph = artist_graph.edges_from(json.loads(args.graph.read_text(encoding="utf-8")))

    def artist_sim(a: str, b: str) -> float:
        pa, pb = set(artist_graph.parts(a)), set(artist_graph.parts(b))
        if pa & pb:
            return 1.0
        return max((graph.get(x, {}).get(y, 0.0) for x in pa for y in pb), default=0.0)

    plays = [
        dict(r)
        for r in con.execute(
            "SELECT path, CAST(strftime('%s', played_at) AS REAL) AS at, skipped, "
            "played_seconds, mode, shuffled FROM plays ORDER BY played_at, id"
        ).fetchall()
    ]
    print(
        f"журнал: {len(plays)} прослушиваний; звучание у {len(space.paths)} треков, "
        f"облаков {len(space.centers)} (рус {space.cloud_language.count(sonic.RU)}), "
        f"медиана расстояния {space.sound_scale:.3f}"
    )

    rows: list[dict[str, Any]] = []
    for prev, cur in pairs(plays):
        mode = _mode(cur)
        i, j = space.index.get(prev["path"]), space.index.get(cur["path"])
        if mode is None or i is None or j is None or prev["path"] == cur["path"]:
            continue
        rows.append(
            {
                "mode": mode,
                "early": early(cur),
                "sound": 1.0 - float(space.matrix[i] @ space.matrix[j]),
                "same_cloud": int(space.cloud[i]) == int(space.cloud[j]),
                "same_lang": space.language[i] == space.language[j],
                "artist": artist_sim(
                    meta.get(prev["path"], {}).get("artist", ""),
                    meta.get(cur["path"], {}).get("artist", ""),
                ),
            }
        )
    rng = random.Random(1)
    edges = np.quantile([r["sound"] for r in rows], [0.25, 0.5, 0.75]) if rows else []

    def bucket(value: float) -> int:
        return int(np.searchsorted(edges, value))

    print(f"\nпар переходов: {len(rows)}; границы четвертей по звуку: {np.round(edges, 3)}")
    for mode in ("smart", "plain", "shuffled", "radio", "blind", "все"):
        sel = [r for r in rows if mode == "все" or r["mode"] == mode]
        if len(sel) < 20:
            continue
        by_q = [[r["early"] for r in sel if bucket(r["sound"]) == q] for q in range(4)]
        near, far = by_q[0] + by_q[1], by_q[2] + by_q[3]
        lo, hi = ci_difference(far, near, rng) if near and far else (0.0, 0.0)
        same = [r["early"] for r in sel if r["same_cloud"]]
        other = [r["early"] for r in sel if not r["same_cloud"]]
        lang_same = [r["early"] for r in sel if r["same_lang"]]
        lang_switch = [r["early"] for r in sel if not r["same_lang"]]
        related = [r["early"] for r in sel if r["artist"] >= 0.5]
        unrelated = [r["early"] for r in sel if r["artist"] < 0.5]
        print(
            f"\n== {mode}: {len(sel)} пар, ранних пропусков {fmt(rate([r['early'] for r in sel]))}"
        )
        print(
            "  по четвертям звука (близко → далеко): "
            + " | ".join(f"{fmt(rate(q))} ({len(q)})" for q in by_q)
        )
        print(
            f"  дальняя половина − ближняя: {(rate(far) or 0) - (rate(near) or 0):+.3f}, "
            f"95 % [{lo:+.3f}, {hi:+.3f}]"
        )
        print(
            f"  то же облако {fmt(rate(same))} ({len(same)}) · "
            f"другое {fmt(rate(other))} ({len(other)})"
        )
        print(
            f"  тот же язык {fmt(rate(lang_same))} ({len(lang_same)}) · "
            f"смена языка {fmt(rate(lang_switch))} ({len(lang_switch)})"
        )
        print(
            f"  артисты похожи {fmt(rate(related))} ({len(related)}) · "
            f"нет {fmt(rate(unrelated))} ({len(unrelated)})"
        )

    # Оценка «что было бы»: распределение расстояний переходов в очередях
    # трёх видов × доля пропусков в этой четверти по всему журналу.
    all_q = [[r["early"] for r in rows if bucket(r["sound"]) == q] for q in range(4)]
    q_rate = [rate(q) or 0.0 for q in all_q]
    tracks = shuffle.tracks_from_rows(index_rows, [], [], 12)
    tracks = [t for t in tracks if t.path in space]
    lessons = sonic.Lessons()

    def expected(queues: list[list]) -> tuple[float, list[float]]:
        counts = [0, 0, 0, 0]
        for queue in queues:
            for a, b in zip(queue, queue[1:], strict=False):
                d = 1.0 - float(
                    space.matrix[space.index[a.path]] @ space.matrix[space.index[b.path]]
                )
                counts[bucket(d)] += 1
        total = sum(counts) or 1
        shares = [c / total for c in counts]
        return sum(s * r for s, r in zip(shares, q_rate, strict=False)), shares

    kinds = {
        "новое умное": [
            sonic.build_session(tracks, space, meta, size=50, seed=s, lessons=lessons)
            for s in range(args.queues)
        ],
        "старое умное": [
            shuffle.build_queue(tracks, size=50, seed=s, use_taste=False)
            for s in range(args.queues)
        ],
        "обычное": [shuffle.plain_shuffle(tracks, size=50, seed=s) for s in range(args.queues)],
    }
    print("\nоценка по расстоянию переходов (если пропуск зависит только от него):")
    for name, queues in kinds.items():
        value, shares = expected(queues)
        report = [sonic.report(q, space, meta) for q in queues if name == "новое умное"]
        extra = ""
        if report:
            extra = (
                f"; артист ближе 6: {sum(r['artist_too_close'] for r in report)}, "
                f"альбом ближе 9: {sum(r['album_too_close'] for r in report)} на "
                f"{sum(r['tracks'] for r in report)} треков"
            )
        print(
            f"  {name:13s}: ожидаемо {fmt(value)}; доли четвертей "
            + " ".join(f"{s * 100:.0f}%" for s in shares)
            + extra
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
