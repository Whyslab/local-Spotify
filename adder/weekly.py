"""Подборка недели — 25 треков не из фонотеки, раз в неделю.

Как «Открытия недели» у Spotify. Собирается ночью в ``scripts/export_shelves.py``,
когда наступила новая неделя (в ночь на понедельник; пропущенная ночь —
значит, на следующую), и лежит в ``runtime.WEEKLY_FILE`` до следующей.

Откуда треки. Кого слушали за последние четыре недели (журнал, без
пропусков) — до двенадцати артистов; их похожие и популярные треки — из
Deezer, тем же подбором, что подмешивает новое в умное перемешивание
(``outside.candidates``). Не больше двух треков одного артиста: подборка —
это разные новые имена, а не дискография одного.

Чего в ней нет. Того, что уже есть в фонотеке, и — пока есть новое — того,
что уже предлагалось за последние полгода и в фонотеку не попало: треки со
стороны в журнал не пишутся, и «отвергнуто» здесь значит «показали и не
взяли». Круг похожих у Deezer конечен, и когда новое кончилось, добирается
предложенное давнее всего. Неделя, когда Deezer дал меньше ``MIN_SIZE``,
оставляет прошлую подборку: полка в пять треков хуже вчерашней.

Слушается подборка как треки со стороны (``outside:<ключ>``): скачиваются
по просьбе плеера, с тем же потолком в час, что у радио, а понравившееся
кнопка «В фонотеку» в плеере отправляет в обычный импорт. Запасной источник
из плана — ListenBrainz — проверен 14.10.2026: для этого пользователя он не
составил ни одной подборки, и брать из него нечего.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from collections import Counter

from . import ingest, outside, runtime, shelves, similar

logger = logging.getLogger(__name__)

SIZE = 25
PER_ARTIST = 2
SEEDS = 12
HISTORY_WEEKS = 26
# Ночью спешить некуда: подбор может ждать Deezer дольше, чем перемешивание.
BUDGET = 60.0
# Сколько треков подборки держать скачанными заранее.
PREFETCH = 5
# Меньше — прошлая подборка остаётся: полка в пять треков хуже вчерашней.
MIN_SIZE = SIZE // 2


def week_of(now: float) -> str:
    year, week, _ = datetime.date.fromtimestamp(now).isocalendar()
    return f"{year}-W{week:02d}"


def load() -> dict:
    try:
        state = json.loads(runtime.WEEKLY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save(state: dict) -> None:
    runtime.WEEKLY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ingest.write_atomic(runtime.WEEKLY_FILE, json.dumps(state, ensure_ascii=False).encode())


def due(now: float | None = None) -> bool:
    return load().get("week") != week_of(time.time() if now is None else now)


def favourites(rows: list[dict]) -> list[str]:
    """Кого слушали за четыре недели (``shelves.favourites``) — до ``SEEDS``."""
    return shelves.favourite_artists(rows, SEEDS)


def build(rows: list[dict], now: float | None = None, budget: float = BUDGET) -> dict:
    """Собрать подборку этой недели. Возвращает, что вышло."""
    now = time.time() if now is None else now
    state = load()
    history: dict[str, list[str]] = {
        week: keys for week, keys in (state.get("history") or {}).items() if isinstance(keys, list)
    }
    offered = {key for keys in history.values() for key in keys}
    seeds, have = favourites(rows), outside.library_keys(rows)
    found = outside.candidates(seeds, have, SIZE * 4, budget=budget, skip=offered)
    picks = _pick(found, [])
    if len(picks) < SIZE and offered:
        # Новых не хватило: круг похожих у Deezer конечен, а ответы его
        # кэшируются навсегда, и через три-четыре месяца всё из круга уже
        # предлагалось. Тогда добираем тем, что предлагалось давнее всего, —
        # полка из двадцати пяти лучше полки из пяти.
        again = outside.candidates(seeds, have, SIZE * 8, budget=budget)
        age = {key: n for n, week in enumerate(sorted(history)) for key in history[week]}
        again.sort(key=lambda item: age.get(item["key"], -1))
        picks = _pick(again, picks)
    if len(picks) < MIN_SIZE:
        return {"built": False, "week": state.get("week"), "count": len(state.get("tracks") or [])}

    week = week_of(now)
    outside.remember(picks)
    history[week] = [item["key"] for item in picks]
    history = dict(sorted(history.items())[-HISTORY_WEEKS:])
    _save({"week": week, "built_at": now, "tracks": picks, "history": history})
    return {"built": True, "week": week, "count": len(picks)}


def _pick(found: list[dict], picks: list[dict]) -> list[dict]:
    """Дописать к ``picks`` из ``found``: не больше ``PER_ARTIST`` на артиста,
    до ``SIZE``, без повторов."""
    picks = list(picks)
    taken = {item["key"] for item in picks}
    per_artist = Counter(similar.primary(item["artist"]).lower() for item in picks)
    for item in found:
        who = similar.primary(item["artist"]).lower()
        if len(picks) >= SIZE:
            break
        if item["key"] in taken or per_artist[who] >= PER_ARTIST:
            continue
        per_artist[who] += 1
        taken.add(item["key"])
        picks.append(item)
    return picks


def _valid(item: object) -> bool:
    """Строка подборки из файла: всё нужное на месте, ключ — тот, что
    выдаёт outside (он становится именем файла в кэше)."""
    return (
        isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and bool(outside._KEY.fullmatch(item["key"]))
        and isinstance(item.get("artist"), str)
        and isinstance(item.get("title"), str)
    )


def current() -> dict:
    """Подборка для главной: строки очереди и состояние скачивания каждой."""
    state = load()
    tracks = state.get("tracks")
    picks = [p for p in tracks if _valid(p)] if isinstance(tracks, list) else []
    # Уборка кэша забывает кандидата, которого неделю не просили скачать, —
    # а подборка живёт неделю. Записанный заново снова можно попросить.
    try:
        outside.remember(picks)
    except OSError as exc:
        logger.warning("Подборка недели: кэш не записан (%s)", exc)
    tracks = [
        {
            **outside.entry(item),
            "cover": item.get("cover") or "",
            "state": outside.status(item["key"]),
        }
        for item in picks
    ]
    return {"week": state.get("week"), "tracks": tracks}


def prefetch(tracks: list[dict]) -> None:
    """Держать ``PREFETCH`` треков скачанными, чтобы «Слушать» было с чего
    начать. Не больше: каждое открытие главной иначе просило бы следующие
    пять, и за неделю скачалась бы вся подборка, даже не слушанная, — заодно
    съев часовой потолок скачиваний у радио."""
    ready = sum(1 for t in tracks if t.get("state") in ("ready", "pending"))
    keys = [
        outside.track_key(t["path"]) or ""
        for t in tracks
        if t.get("state") in ("wanted", "unknown")
    ]
    if ready < PREFETCH and keys:
        outside.request(keys[: PREFETCH - ready])
