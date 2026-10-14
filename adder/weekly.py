"""Подборка недели — 25 треков не из фонотеки, раз в неделю.

Как «Открытия недели» у Spotify. Собирается ночью в ``scripts/export_shelves.py``,
когда наступила новая неделя (в ночь на понедельник; пропущенная ночь —
значит, на следующую), и лежит в ``runtime.WEEKLY_FILE`` до следующей.

Откуда треки. Кого слушали за последние четыре недели (журнал, без
пропусков) — до двенадцати артистов; их похожие и популярные треки — из
Deezer, тем же подбором, что подмешивает новое в умное перемешивание
(``outside.candidates``). Не больше двух треков одного артиста: подборка —
это разные новые имена, а не дискография одного.

Чего в ней нет. Того, что уже есть в фонотеке, и того, что уже предлагалось
за последние полгода и в фонотеку не попало: треки со стороны в журнал не
пишутся, и «отвергнуто» здесь значит «показали и не взяли». Неделя, когда
Deezer молчит, оставляет прошлую подборку: пустая полка хуже вчерашней.

Слушается подборка как треки со стороны (``outside:<ключ>``): скачиваются
по просьбе плеера, с тем же потолком в час, что у радио, а понравившееся
кнопка «В фонотеку» в плеере отправляет в обычный импорт. Запасной источник
из плана — ListenBrainz — проверен 14.10.2026: для этого пользователя он не
составил ни одной подборки, и брать из него нечего.
"""

from __future__ import annotations

import datetime
import json
import time
from collections import Counter

from . import ingest, outside, runtime, shelves, similar

SIZE = 25
PER_ARTIST = 2
SEEDS = 12
HISTORY_WEEKS = 26
# Ночью спешить некуда: подбор может ждать Deezer дольше, чем перемешивание.
BUDGET = 60.0
# Сколько первых не скачанных треков главная просит скачать заранее.
PREFETCH = 5


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
    found = outside.candidates(
        favourites(rows), outside.library_keys(rows), SIZE * 4, budget=budget
    )

    picks: list[dict] = []
    per_artist: Counter[str] = Counter()
    for item in found:
        who = similar.primary(item["artist"]).lower()
        if item["key"] in offered or per_artist[who] >= PER_ARTIST:
            continue
        per_artist[who] += 1
        picks.append(item)
        if len(picks) >= SIZE:
            break
    if not picks:
        return {"built": False, "week": state.get("week"), "count": len(state.get("tracks") or [])}

    week = week_of(now)
    outside.remember(picks)
    history[week] = [item["key"] for item in picks]
    history = dict(sorted(history.items())[-HISTORY_WEEKS:])
    _save({"week": week, "built_at": now, "tracks": picks, "history": history})
    return {"built": True, "week": week, "count": len(picks)}


def current() -> dict:
    """Подборка для главной: строки очереди и состояние скачивания каждой."""
    state = load()
    picks = [p for p in state.get("tracks") or [] if isinstance(p, dict) and p.get("key")]
    # Уборка кэша забывает кандидата, которого неделю не просили скачать, —
    # а подборка живёт неделю. Записанный заново снова можно попросить.
    outside.remember(picks)
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
    """Первые не скачанные — скачать заранее, чтобы «Слушать» было с чего начать."""
    keys = [
        outside.track_key(t["path"]) or ""
        for t in tracks
        if t.get("state") in ("wanted", "unknown")
    ]
    if keys:
        outside.request(keys[:PREFETCH])
