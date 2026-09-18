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


def _find_artist(client: httpx.Client, name: str) -> dict | None:
    """Артист Deezer по имени. None — «не знает такого», иначе `_Unreachable`."""
    found = client.get(SEARCH, params={"q": name, "limit": 5})
    if not found.is_success:
        raise _Unreachable(f"поиск ответил {found.status_code}")
    candidates = (found.json() or {}).get("data") or []
    if not candidates:
        return None

    # Поиск по строке иногда попадает не в того: на «PHARAOH» нашёлся
    # однофамилец без похожих. Точное совпадение имени надёжнее
    # первого места в выдаче.
    exact = [c for c in candidates if c.get("name", "").lower() == name.lower()]
    return (exact or candidates)[0]


def _ask(name: str, limit: int) -> list[str] | None:
    """Спросить Deezer. None значит «не дозвонились», [] — «не знает»."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            artist = _find_artist(client, name)
            if artist is None:
                return []

            related = client.get(RELATED.format(id=artist["id"]), params={"limit": limit})
            if not related.is_success:
                return None
            return [row["name"] for row in (related.json() or {}).get("data") or []]
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

            top = client.get(TOP.format(id=artist["id"]), params={"limit": limit})
            if not top.is_success:
                return None
            tracks = []
            for row in (top.json() or {}).get("data") or []:
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                # Имя артиста берём из трека, а не из найденного артиста:
                # у совместного трека в топе стоит тот, кто его выпустил.
                who = ((row.get("artist") or {}).get("name") or artist.get("name") or "").strip()
                tracks.append({"artist": who, "title": title})
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
    """Популярные треки артиста: `[{"artist": ..., "title": ...}]`.

    Нужны, чтобы у похожего артиста было что предложить по имени: имени мало,
    человек ищет трек. Кэш такой же вечный, как у похожих: чарт артиста живёт
    неделями, а спрашивают его на каждое открытие очереди.
    """
    name = primary(artist)
    if len(name) < 2:
        return []

    slot = _slot(cache_dir, name, kind="top:")
    cached = _recall(slot, "tracks")
    if cached is not None:
        return cached

    tracks = _ask_top(name, limit)
    if tracks is None:
        return []

    _remember(slot, cache_dir, name, "tracks", tracks)
    return tracks
