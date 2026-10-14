"""Бэктест умного перемешивания на журнале прослушиваний.

Для каждого прослушивания из журнала восстанавливаем, что знал бы
перемешиватель в ту минуту (журнал до него, час, игравший перед ним трек), и
смотрим, как высоко он поставил бы реально сыгранный трек среди всей
фонотеки: старой оценкой (без вкуса) и новой (``shuffle.taste_weight``).
Число — перцентиль: 1.0 — выше всех, 0.0 — ниже всех. Хорошая оценка ставит
пропущенное ниже, а дослушанное выше; «разрыв» — разница между ними.

Базу открывает только на чтение и ничего не пишет.

    .venv/bin/python scripts/backtest_shuffle.py         # весь журнал
    .venv/bin/python scripts/backtest_shuffle.py --fit   # подобрать на первой
                                                         # половине, оценить на второй
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adder import shuffle  # noqa: E402

# Что подбирает --fit. Остальное в оценке — как в shuffle.py.
GRID = {
    "SKIP_PENALTY": (0.5, 1.5, 3.0),
    "HEARD_BONUS": (0.0, 0.5, 1.0),
    "ARTIST_PENALTY": (0.0, 1.5, 3.0),
    "TASTE_HALF_LIFE": (50, 150, 400),
}


def _percentile(weights: list[float], target: float) -> float:
    below = sum(1 for w in weights if w < target)
    ties = sum(1 for w in weights if w == target)
    # Сам трек среди равных тоже есть; ничья делится пополам.
    return (below + (ties - 1) / 2) / max(len(weights) - 1, 1)


def evaluate(
    index_rows: list[dict],
    feature_rows: list[dict],
    plays: list[dict],
    start: int = 0,
    stop: int | None = None,
    use_taste: bool = True,
) -> dict:
    """Средний перцентиль пропущенных и дослушанных в ``plays[start:stop]``.

    ``plays`` — по времени, старые первыми; у строки ``path``, ``hour``,
    ``skipped``, ``played_seconds``, ``duration``.
    """
    stop = len(plays) if stop is None else stop
    known = {row["path"] for row in index_rows}
    skipped: list[float] = []
    heard: list[float] = []
    for i in range(start, stop):
        play = plays[i]
        if play["path"] not in known:
            continue
        is_skip = bool(play.get("skipped"))
        if not is_skip and not shuffle._heard_through(play):
            continue
        history = plays[:i][::-1]
        tracks = shuffle.tracks_from_rows(index_rows, feature_rows, history, int(play["hour"]))
        by_path = {t.path: t for t in tracks}
        before = by_path.get(plays[i - 1]["path"]) if i else None
        weights = {
            t.path: shuffle.candidate_weight(before, t, use_taste=use_taste)
            for t in tracks
            if before is None or t.path != before.path
        }
        if play["path"] not in weights:
            continue
        rank = _percentile(list(weights.values()), weights[play["path"]])
        (skipped if is_skip else heard).append(rank)

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    low, high = mean(skipped), mean(heard)
    result: dict = {"skipped": low, "heard": high, "n_skipped": len(skipped), "n_heard": len(heard)}
    if low is not None and high is not None:
        result["gap"] = round(high - low, 4)
    return result


@contextmanager
def params(values: dict) -> Iterator[None]:
    """На время — другие параметры вкуса в ``shuffle``."""
    saved = {name: getattr(shuffle, name) for name in values}
    try:
        for name, value in values.items():
            setattr(shuffle, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(shuffle, name, value)


def fit(index_rows: list[dict], feature_rows: list[dict], plays: list[dict], stop: int) -> dict:
    """Параметры с наибольшим разрывом на ``plays[:stop]``."""
    best: tuple[float, dict] | None = None
    for combo in itertools.product(*GRID.values()):
        values = dict(zip(GRID, combo, strict=True))
        with params(values):
            gap = evaluate(index_rows, feature_rows, plays, 0, stop).get("gap")
        if gap is not None and (best is None or gap > best[0]):
            best = (gap, values)
    return best[1] if best else {}


def _load(db_path: Path) -> tuple[list[dict], list[dict]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        features = [
            dict(r)
            for r in con.execute(
                "SELECT path, tempo, energy, brightness, music_key FROM audio_features"
            )
        ]
        plays = [
            dict(r)
            for r in con.execute(
                "SELECT path, CAST(strftime('%H', played_at) AS INTEGER) AS hour, skipped, "
                "played_seconds, duration FROM plays ORDER BY played_at, id"
            )
        ]
    finally:
        con.close()
    return features, plays


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--fit", action="store_true", help="подобрать параметры на первой половине")
    parser.add_argument("--db", type=Path, help="база (по умолчанию — служебная adder.db)")
    args = parser.parse_args()

    from adder import library, runtime

    features, plays = _load(args.db or runtime.DB_PATH)
    index = library.library_index()
    half = len(plays) // 2
    report: dict = {"plays": len(plays), "tracks": len(index)}
    if args.fit:
        chosen = fit(index, features, plays, half)
        report["fitted_on"] = f"0..{half}"
        report["params"] = chosen
        report["old"] = evaluate(index, features, plays, half, use_taste=False)
        with params(chosen):
            report["new"] = evaluate(index, features, plays, half)
        report["evaluated_on"] = f"{half}..{len(plays)}"
    else:
        report["old"] = evaluate(index, features, plays, use_taste=False)
        report["new"] = evaluate(index, features, plays)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
