"""Радио: очередь без конца от одного трека или артиста.

Включил «Радио» у трека — дальше играет тот же круг: сам артист, похожие на
него (по Deezer, ``similar``) из своей фонотеки и примерно каждый третий трек
не из неё (``outside``), как в умном перемешивании. Очередь приходит порциями
по двадцать: плеер просит следующую, когда в нынешней остаётся меньше пяти.

Что не повторяется. Последние пятьдесят треков радио плеер присылает сам — в
каждой следующей порции их нет. Пропущенное за последние две недели (если
после пропуска трек не дослушали) радио не предлагает вовсе: раз уж его
выключили на середине, ставить его снова — значит раздражать.

Переходы — те же, что у умного перемешивания (``shuffle.build_queue``): по
темпу и энергии, и первым в порции встаёт трек, который продолжает
игравший.
"""

from __future__ import annotations

import random
import time

from . import db, outside, shuffle, similar

PORTION = 20
# Сколько последних треков радио не повторяется. Столько плеер и присылает.
WINDOW = 50
SKIP_DAYS = 14
NEIGHBOURS = 12
# Круг меньше порции — спрашиваем похожих на похожих. Ответы кэшируются
# навсегда (similar), так что ждать приходится только в первый раз.
SECOND_LEVEL = 6
SECOND_LEVEL_BUDGET = 4.0


def _name(artist: str) -> str:
    return " ".join(outside._norm(similar.primary(artist)).split())


def recently_skipped(days: int = SKIP_DAYS) -> set[str]:
    """Пути, последнее прослушивание которых за ``days`` дней — пропуск."""
    rows = db.db_query(
        "SELECT path, skipped FROM plays WHERE played_at >= datetime('now', 'localtime', ?) "
        "ORDER BY played_at, id",
        (f"-{days} days",),
    )
    last: dict[str, bool] = {}
    for row in rows:
        last[row["path"]] = bool(row["skipped"])
    return {path for path, skipped in last.items() if skipped}


def station(seed_artist: str, need: int, tracks: list[shuffle.Track]) -> set[str]:
    """Имена круга: сам артист, похожие на него и, если своих в фонотеке
    меньше ``need``, похожие на похожих."""
    cache = outside._similar_cache()
    neighbours = similar.similar_artists(seed_artist, cache, limit=NEIGHBOURS)
    names = {_name(seed_artist), *(_name(n) for n in neighbours)}
    names.discard("")
    if sum(1 for t in tracks if _name(t.artist) in names) >= need:
        return names
    more = outside._gather(
        neighbours[:SECOND_LEVEL],
        lambda name: similar.similar_artists(name, cache)[:NEIGHBOURS],
        time.monotonic() + SECOND_LEVEL_BUDGET,
    )
    for found in more.values():
        names.update(_name(n) for n in found)
    names.discard("")
    return names


def build(
    tracks: list[shuffle.Track],
    seed_artist: str,
    recent: list[str],
    previous: str | None = None,
    size: int = PORTION,
    have: set[tuple[str, str]] | None = None,
    skipped: set[str] | None = None,
    seed: int | None = None,
) -> tuple[list[shuffle.Track], list[dict]]:
    """Порция радио: свои треки по порядку и найденные со стороны.

    Своё вплетает со стороной вызывающий (``outside.weave``) — ему же нужны
    строки очереди из фонотеки. ``have`` — ключи фонотеки для
    ``outside.candidates``; ``None`` — со стороны не брать.
    """
    rng = random.Random(seed)
    banned = set(recent[-WINDOW:]) | (skipped or set())
    if previous:
        banned.add(previous)
    eligible = [t for t in tracks if t.path not in banned]
    if not eligible:
        return [], []

    want = round(size * outside.SHARE) if have is not None else 0
    names = station(seed_artist, size, eligible)
    core = {t.path for t in eligible if _name(t.artist) in names}

    by_path = {t.path: t for t in tracks}
    before = by_path.get(previous) if previous else None
    opening: list[shuffle.Track] = []
    if before is None:
        # Радио артиста начинается с него самого — без «предыдущего» иначе
        # первым мог встать любой из похожих.
        own = [t for t in eligible if _name(t.artist) == _name(seed_artist)]
        if own:
            first = rng.choices(own, weights=[shuffle._coverage_weight(t) for t in own], k=1)[0]
            opening, before = [first], first

    def local_part(count: int) -> list[shuffle.Track]:
        rest = count - len(opening)
        if rest <= 0:
            return opening[:count]
        return opening + shuffle.build_queue(
            eligible,
            size=rest,
            core=core or None,
            outside_share=0.0,
            previous=before,
            seed=rng.randrange(2**32),
        )

    local = local_part(size - want)
    found: list[dict] = []
    if want:
        gone = {outside.track_key(p) for p in banned if outside.is_outside(p)}
        artists = [seed_artist, *(t.artist for t in local)]
        asked = outside.candidates(artists, have or set(), want * 3)
        found = [c for c in asked if c["key"] not in gone][:want]
        if len(found) < want:
            # Нашлось меньше, чем мест под новое, — своих больше, чтобы порция
            # не укорачивалась.
            local = local_part(size - len(found))
    return local, found
