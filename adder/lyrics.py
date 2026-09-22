"""Тексты песен для плеера: спрашиваем один раз, храним на диске.

Источник — LRCLIB (lrclib.net). Ни ключа, ни регистрации, и, что важнее, он
знает эту фонотеку: проба по 25 случайным трекам нашла 15, двенадцать из них
с таймингами, включая русский рэп, которого нет в западных каталогах.

Промах кладётся в кэш тоже, но на срок покороче: у трека, которого сегодня
нет, текст может появиться через месяц, а спрашивать при каждом воспроизведении
и долго, и невежливо по отношению к чужому бесплатному сервису.

Кэш живёт в adder/lyrics-cache — единственном месте под проектом, куда служба
может писать (см. ReadWritePaths в юните).
"""

import hashlib
import json
import logging
import re
import time
from pathlib import Path

import requests

from . import library, runtime

logger = logging.getLogger(__name__)

CACHE_DIR = runtime.PROJECT / "lyrics-cache"
API = "https://lrclib.net/api/get"
TIMEOUT = 8
MISS_TTL = 14 * 24 * 3600  # промах перепроверяем через две недели
AGENT = "local-Spotify (personal music library, https://github.com/Whyslab/local-Spotify)"

# [mm:ss.xx] или [mm:ss] в начале строки
_STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _key(rel_path: str) -> Path:
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def primary_artist(artist: str) -> str:
    """Первый артист из строки.

    В тегах этой фонотеки фиты склеены через «•» — «ALBLAK 52 • Скриптонит».
    Каталог такую строку целиком не знает, а первого артиста знает.
    """
    value = (artist or "").split("•")[0]
    for marker in (" feat", " ft.", " с участием"):
        if marker in value.lower():
            value = value[: value.lower().index(marker)]
    return value.strip()


def clean_title(title: str) -> str:
    """Название без хвостов вроде «[prod. by ...]» — по ним поиск промахивается."""
    value = title or ""
    for opener in (" [prod", " (prod", " - prod", " [Prod", " (Prod"):
        if opener in value:
            value = value[: value.index(opener)]
    return value.strip()


def parse_synced(text: str) -> list[dict]:
    """Разобрать LRC в список строк с временем.

    У одной реплики бывает несколько меток времени — тогда она повторяется
    в каждый из этих моментов, как и задумано форматом.
    """
    out: list[dict] = []
    for raw in (text or "").splitlines():
        stamps = list(_STAMP.finditer(raw))
        if not stamps:
            continue
        line = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            at = int(minutes) * 60 + int(seconds)
            if fraction:
                at += int(fraction) / (10 ** len(fraction))
            out.append({"at": round(at, 2), "line": line})
    out.sort(key=lambda item: item["at"])
    return out


def _ask(artist: str, title: str, album: str, duration: float | None) -> dict | None:
    params = {
        "artist_name": primary_artist(artist),
        "track_name": clean_title(title),
        "album_name": album or "",
    }
    if duration:
        params["duration"] = int(duration)
    try:
        response = requests.get(
            API, params=params, timeout=TIMEOUT, headers={"User-Agent": AGENT}
        )
    except requests.RequestException as exc:
        logger.info("lyrics: %s — %s", title, exc)
        return None
    if response.status_code == 404:
        return {}
    if not response.ok:
        # 503 значит «слишком часто» — это не «текста нет», и кэшировать нельзя
        logger.info("lyrics: %s — ответ %s", title, response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        return None


def for_track(rel_path: str, row: dict | None = None) -> dict:
    """Текст трека: из кэша, а если там пусто — спросить и запомнить.

    ``row`` — теги трека, которого нет в фонотеке (трек со стороны в умном
    перемешивании). Без него теги берутся из индекса фонотеки.
    """
    path = _key(rel_path)
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            fresh = cached.get("found") or (time.time() - cached.get("at", 0)) < MISS_TTL
            if fresh:
                return cached
        except (ValueError, OSError):
            pass  # битый кэш — просто спросим заново

    if row is None:
        row = next((r for r in library.library_index() if r["path"] == rel_path), None)
    if row is None:
        return {"found": False, "reason": "Трека нет в фонотеке"}

    data = _ask(row["artist"], row["title"], row["album"], row["duration"])
    if data is None:
        # Сеть не ответила. Не запоминаем: это не про этот трек.
        return {"found": False, "reason": "Каталог текстов не ответил"}

    synced = parse_synced(data.get("syncedLyrics") or "")
    plain = (data.get("plainLyrics") or "").strip()
    result = {
        "found": bool(synced or plain),
        "at": time.time(),
        "synced": synced,
        "plain": plain,
        "source": "lrclib" if (synced or plain) else "",
        "title": row["title"],
        "artist": row["artist"],
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("lyrics: не смог записать кэш: %s", exc)
    return result
