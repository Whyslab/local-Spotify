"""«Похожие треки» в конце подборки — как у Spotify под плейлистом.

Две части, и плеер спрашивает их отдельно: своя приходит сразу, а новая ждёт
Deezer и не должна держать первую.

* ``from_library`` — треки фонотеки, которых в подборке нет, по звучанию
  (эмбеддинги ``sonic``). Не по «среднему» подборки: у «Любимого» тысяча
  треков, и середина их — ни на что не похожая точка. Каждый кандидат
  получает среднее сходство с ``NEAREST`` самыми близкими треками подборки:
  так подходит трек, похожий хоть на часть подборки, а не на всю сразу.
  Звучание ещё не посчитано — по артистам подборки.
* ``from_outside`` — два-три трека не из фонотеки у артистов подборки и
  похожих на них (``outside.candidates``, как в умном и радио). Слушать — тем
  же путём, что подборку недели; понравилось — «В фонотеку» в плеере.

Что не предлагается: уже в подборке, пропущенное за две недели
(``radio.recently_skipped``), больше ``PER_ARTIST`` треков одного артиста.
"""

from __future__ import annotations

import logging
import random
import re
import zlib
from collections import Counter

import numpy as np

from . import outside, similar, sonic

logger = logging.getLogger(__name__)

LIMIT = 12
PER_ARTIST = 2
NEAREST = 5
# Подборку больше — сравниваем с выборкой: тысяча на три тысячи — уже
# заметные доли секунды на слабом сервере, а точнее от этого не становится.
MEMBERS_MAX = 400
OUTSIDE_WANT = 3
OUTSIDE_SEEDS = 6
OUTSIDE_BUDGET = 6.0


def _artist(row: dict) -> str:
    return " ".join(outside._norm(similar.primary(row.get("artist") or "")).split())


def _entry(row: dict) -> dict:
    return {
        "path": row["path"],
        "title": row.get("title") or "",
        "artist": row.get("artist") or "",
        "album": row.get("album") or "",
        "duration": row.get("duration"),
    }


def _everyone(row: dict) -> set[str]:
    """Все артисты трека: «IROH • GONE.Fludd» — оба."""
    names = (row.get("artist") or "").split("•")
    return {" ".join(outside._norm(name).split()) for name in names} - {""}


def _song(title: str) -> str:
    """Песня без приписок: всё до первой скобки или « - » — версия ли это,
    фит или год, у одного артиста это та же песня."""
    head = re.split(r"\s[-–—]\s|[(\[]", title.lower(), maxsplit=1)[0]
    return " ".join(outside._norm(head).split())


def _capped(order: list[dict], limit: int) -> list[dict]:
    """Первые ``limit``: не больше ``PER_ARTIST`` на артиста и одна версия песни.

    Считаются все участники, не только первый: 24.11.2026 на живой копии
    шесть из двенадцати были с GONE.Fludd — то первым, то на фите. И одна
    песня — один раз: «I CAN KICK IT (feat. bbno$)» и «I CAN KICK IT ( , 2020)»
    стояли рядом.
    """
    per: Counter[str] = Counter()
    songs: set[tuple[str, str]] = set()
    out = []
    for row in order:
        who = _everyone(row)
        song = (_artist(row), _song(row.get("title") or ""))
        if any(per[name] >= PER_ARTIST for name in who) or (song[1] and song in songs):
            continue
        per.update(who)
        songs.add(song)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def from_library(
    members: list[str],
    rows: list[dict],
    space: sonic.Space | None,
    skipped: set[str] | None = None,
    limit: int = LIMIT,
    seed: str = "",
) -> list[dict]:
    """Похожее из фонотеки на подборку с путями ``members``."""
    inside = set(members)
    skipped = skipped or set()
    pool = [
        row
        for row in rows
        if row.get("path")
        and row["path"] not in inside
        and row["path"] not in skipped
        and not outside.is_outside(row["path"])
    ]
    if not inside or not pool:
        return []
    known = [p for p in members if space is not None and p in space]
    if space is not None and len(known) >= 3:
        if len(known) > MEMBERS_MAX:
            # Выборка — одна и та же для подборки: список не прыгает от захода к заходу.
            known = random.Random(zlib.crc32(seed.encode())).sample(known, MEMBERS_MAX)
        heard = [row for row in pool if row["path"] in space]
        if heard:
            m = space.matrix[[space.index[p] for p in known]]
            c = space.matrix[[space.index[row["path"]] for row in heard]]
            sims = c @ m.T  # строки единичные: это косинус
            k = min(NEAREST, sims.shape[1])
            top = np.partition(sims, sims.shape[1] - k, axis=1)[:, -k:]
            score = top.mean(axis=1)
            order = [heard[i] for i in np.argsort(-score, kind="stable")]
            return [_entry(row) for row in _capped(order, limit)]
    # Звучания нет — артисты подборки, начиная с самых частых в ней.
    by_path = {row["path"]: row for row in rows}
    count = Counter(_artist(by_path[p]) for p in members if p in by_path)
    count.pop("", None)
    rng = random.Random(zlib.crc32(seed.encode()))
    mine = [row for row in pool if count.get(_artist(row))]
    rng.shuffle(mine)
    mine.sort(key=lambda row: -count[_artist(row)])
    return [_entry(row) for row in _capped(mine, limit)]


def seed_artists(members: list[str], rows: list[dict], limit: int = OUTSIDE_SEEDS) -> list[str]:
    """Артисты подборки, самые частые первыми — от них ищется новое."""
    by_path = {row["path"]: row for row in rows}
    count: Counter[str] = Counter()
    names: dict[str, str] = {}
    for path in members:
        row = by_path.get(path)
        if not row or not row.get("artist"):
            continue
        key = _artist(row)
        count[key] += 1
        names.setdefault(key, similar.primary(row["artist"]))
    return [names[key] for key, _ in count.most_common(limit) if key]


def from_outside(members: list[str], rows: list[dict], want: int = OUTSIDE_WANT) -> list[dict]:
    """Два-три трека не из фонотеки — записи очереди, как у радио."""
    seeds = seed_artists(members, rows)
    if not seeds:
        return []
    found = outside.candidates(seeds, outside.library_keys(rows), want * 3, budget=OUTSIDE_BUDGET)
    found = outside.one_per_artist(found)[:want]
    if not found:
        return []
    outside.remember(found)
    out = []
    for item in found:
        entry = outside.entry(item)
        entry["cover"] = item.get("cover") or ""
        entry["state"] = outside.status(item["key"])
        out.append(entry)
    return out
