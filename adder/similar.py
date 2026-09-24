"""Похожие артисты — чтобы в очередь попадало то, о чём не думал.

Источник — Deezer. Ни ключа, ни регистрации, и, что решало всё, **он знает
эту фонотеку**: на пробе 12.09.2026 по «FRIENDLY THUG 52 NGG» (6 643
слушателя) он назвал ALBLAK 52, Big Baby Tape, Pharaoh, Oxxxymiron, Face
и OBLADAET. Для нишевого русского рэпа это точное попадание, а именно из
него фонотека в основном и состоит.

Запасной источник — ListenBrainz, он тоже знает этих артистов, но требует
сперва найти идентификатор в MusicBrainz, а тот пускает не чаще раза
в секунду. Здесь он не используется; если Deezer однажды закроется,
переключаться туда.

Ответ кладётся на диск навсегда: соседство артистов меняется годами,
а не днями, и спрашивать одно и то же при каждом перемешивании незачем.
Промах тоже запоминается — на месяц.

Отсюда же берутся популярные треки артиста (`top_tracks`): одного имени мало,
чтобы что-то предложить человеку, — ему нужен трек, который можно поискать.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

SEARCH = "https://api.deezer.com/search/artist"
RELATED = "https://api.deezer.com/artist/{id}/related"
TOP = "https://api.deezer.com/artist/{id}/top"
TIMEOUT = 8
MISS_TTL = 30 * 24 * 3600


class _Unreachable(Exception):
    """Deezer не ответил. Отличать это от «не знает такого» приходится потому,
    что промах можно запомнить, а обрыв связи запоминать нельзя: завтра тот же
    артист найдётся."""


def _slot(cache_dir: Path, name: str, kind: str = "") -> Path:
    """Файл кэша. Про одного артиста спрашивают о разном, и у каждого вопроса
    свой файл: иначе похожие и топ-треки затирали бы друг друга. Похожие
    остаются без приставки — их кэш уже лежит на диске и переспрашивать незачем.
    """
    digest = hashlib.sha1(f"{kind}{name.strip().lower()}".encode()).hexdigest()
    return cache_dir / f"{digest}.json"


def _recall(slot: Path, field: str) -> list | None:
    """Что лежит в кэше. None значит «нечего взять, надо спрашивать»."""
    if not slot.exists():
        return None
    try:
        cached = json.loads(slot.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    saved = cached.get(field) or []
    if saved or (time.time() - cached.get("at", 0)) < MISS_TTL:
        return list(saved)
    return None


def _remember(slot: Path, cache_dir: Path, name: str, field: str, value: list) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        slot.write_text(
            json.dumps({"artist": name, field: value, "at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Deezer: не смог записать кэш: %s", exc)


def primary(artist: str) -> str:
    """Первый артист строки: в тегах фиты склеены через «•»."""
    return (artist or "").split("•")[0].split(" feat")[0].strip()


# Deezer пускает около 50 запросов за 5 секунд. Умное перемешивание спрашивает
# про десятки артистов сразу, поэтому запросы из всех потоков идут не чаще
# этого интервала — иначе квота кончается посреди подбора.
MIN_INTERVAL = 0.15
_pace_lock = threading.Lock()
_last_request = 0.0


def _get(client: httpx.Client, url: str, params: dict) -> list:
    """`data` ответа Deezer. Любой сбой — `_Unreachable`, а не пустой список.

    Превышение квоты Deezer отдаёт с кодом 200: внутри `{"error": {...}}` и
    никакого `data`. Прочитанное как «ничего нет», оно попадало в кэш на месяц
    — у двух десятков артистов пропали треки после одного перемешивания.
    """
    global _last_request
    with _pace_lock:
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    response = client.get(url, params=params)
    if not response.is_success:
        raise _Unreachable(f"ответ {response.status_code}")
    try:
        body = response.json() or {}
    except ValueError as exc:
        raise _Unreachable("ответ не JSON") from exc
    if "error" in body:
        raise _Unreachable(f"ошибка Deezer: {body['error']}")
    return body.get("data") or []


def _find_artist(client: httpx.Client, name: str) -> dict | None:
    """Артист Deezer по имени. None — «не знает такого», иначе `_Unreachable`."""
    candidates = _get(client, SEARCH, {"q": name, "limit": 5})
    if not candidates:
        return None

    # Поиск по строке иногда попадает не в того: на «PHARAOH» нашёлся
    # однофамилец без похожих. Точное совпадение имени надёжнее
    # первого места в выдаче, а из тёзок берём того, у кого больше слушателей.
    exact = [c for c in candidates if c.get("name", "").lower() == name.lower()]
    if exact:
        return max(exact, key=lambda c: c.get("nb_fan") or 0)
    return candidates[0]


def _ask(name: str, limit: int) -> list[str] | None:
    """Спросить Deezer. None значит «не дозвонились», [] — «не знает»."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            artist = _find_artist(client, name)
            if artist is None:
                return []

            related = _get(client, RELATED.format(id=artist["id"]), {"limit": limit})
            return [row["name"] for row in related]
    except (_Unreachable, httpx.HTTPError) as exc:
        logger.info("похожие для %s: %s", name, exc)
        return None


def _ask_top(name: str, limit: int) -> list[dict] | None:
    """То же про популярные треки. None — «не дозвонились», [] — «не знает»."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            artist = _find_artist(client, name)
            if artist is None:
                return []

            tracks = []
            for row in _get(client, TOP.format(id=artist["id"]), {"limit": limit}):
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                # Имя артиста берём из трека, а не из найденного артиста:
                # у совместного трека в топе стоит тот, кто его выпустил.
                who = ((row.get("artist") or {}).get("name") or artist.get("name") or "").strip()
                album = row.get("album") or {}
                tracks.append(
                    {
                        "artist": who,
                        "title": title,
                        # Длительность — то, по чему на YouTube отличают
                        # студийную запись от клипа со вставками и концертника.
                        "duration": row.get("duration") or None,
                        "album": (album.get("title") or "").strip(),
                        "cover": album.get("cover_big") or album.get("cover_medium") or "",
                    }
                )
            return tracks
    except (_Unreachable, httpx.HTTPError) as exc:
        logger.info("топ-треки для %s: %s", name, exc)
        return None


def similar_artists(artist: str, cache_dir: Path, limit: int = 12) -> list[str]:
    """Кого Deezer считает похожим. Пустой список — значит не знает."""
    name = primary(artist)
    if len(name) < 2:
        return []

    slot = _slot(cache_dir, name)
    cached = _recall(slot, "names")
    if cached is not None:
        return cached

    names = _ask(name, limit)
    if names is None:
        # Сеть молчит. Не запоминаем: это не про этого артиста.
        return []

    _remember(slot, cache_dir, name, "names", names)
    return names


def top_tracks(artist: str, cache_dir: Path, limit: int = 5) -> list[dict]:
    """Популярные треки артиста: `[{"artist", "title", "duration", "album", "cover"}]`.

    Нужны, чтобы у похожего артиста было что предложить по имени: имени мало,
    человек ищет трек. Кэш такой же вечный, как у похожих: чарт артиста живёт
    неделями, а спрашивают его на каждое открытие очереди.
    """
    name = primary(artist)
    if len(name) < 2:
        return []

    # «top2:», а не «top:»: в старом кэше нет длительностей, а без них
    # умное перемешивание не может выбрать версию на YouTube.
    slot = _slot(cache_dir, name, kind="top2:")
    cached = _recall(slot, "tracks")
    if cached is not None:
        return cached

    tracks = _ask_top(name, limit)
    if tracks is None:
        return []

    _remember(slot, cache_dir, name, "tracks", tracks)
    return tracks
