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
TIMEOUT = 8
MISS_TTL = 30 * 24 * 3600


def _slot(cache_dir: Path, name: str) -> Path:
    digest = hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def primary(artist: str) -> str:
    """Первый артист строки: в тегах фиты склеены через «•»."""
    return (artist or "").split("•")[0].split(" feat")[0].strip()


def _ask(name: str, limit: int) -> list[str] | None:
    """Спросить Deezer. None значит «не дозвонились», [] — «не знает»."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            found = client.get(SEARCH, params={"q": name, "limit": 5})
            if not found.is_success:
                return None
            candidates = (found.json() or {}).get("data") or []
            if not candidates:
                return []

            # Поиск по строке иногда попадает не в того: на «PHARAOH» нашёлся
            # однофамилец без похожих. Точное совпадение имени надёжнее
            # первого места в выдаче.
            exact = [c for c in candidates if c.get("name", "").lower() == name.lower()]
            artist = (exact or candidates)[0]

            related = client.get(RELATED.format(id=artist["id"]), params={"limit": limit})
            if not related.is_success:
                return None
            return [row["name"] for row in (related.json() or {}).get("data") or []]
    except httpx.HTTPError as exc:
        logger.info("похожие для %s: %s", name, exc)
        return None


def similar_artists(artist: str, cache_dir: Path, limit: int = 12) -> list[str]:
    """Кого Deezer считает похожим. Пустой список — значит не знает."""
    name = primary(artist)
    if len(name) < 2:
        return []

    slot = _slot(cache_dir, name)
    if slot.exists():
        try:
            cached = json.loads(slot.read_text(encoding="utf-8"))
            fresh = cached.get("names") or (time.time() - cached.get("at", 0)) < MISS_TTL
            if fresh:
                return list(cached.get("names") or [])
        except (ValueError, OSError):
            pass

    names = _ask(name, limit)
    if names is None:
        # Сеть молчит. Не запоминаем: это не про этого артиста.
        return []

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        slot.write_text(
            json.dumps({"artist": name, "names": names, "at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("похожие: не смог записать кэш: %s", exc)
    return names
