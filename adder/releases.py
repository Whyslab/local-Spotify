"""Новые релизы артистов фонотеки — полка «Новые релизы» на главной.

Раз в ночь (``scripts/export_shelves.py``, таймер ``music-shelves``) для
артистов фонотеки — самых крупных, не больше ``MAX_ARTISTS`` — спрашиваем
Deezer, что у них вышло за последние ``RECENT_DAYS`` дней, и кладём
найденное в ``runtime.NEW_RELEASES_FILE``. Главная читает файл как есть:
ни одного запроса наружу при её открытии.

Кого спрашиваем. Только артиста, которого Deezer знает под **тем же**
именем (``discography.find_artist``): в фонотеке есть каналы YouTube вместо
артистов, и по такому имени Deezer назвал бы чужие релизы. Номер артиста
запоминается навсегда; «не нашёлся» — на месяц, потом ищем снова.

Что новое. Альбом, EP или сингл не старше ``RECENT_DAYS``, не версия
(ремикс, sped up — как в «Артисте целиком») и не то, что уже лежит в
фонотеке: альбом с тем же названием или трек с названием сингла у того же
артиста. Уведомление на рабочем столе — только о впервые увиденном.

Скачивать само — настройка ``NEW_RELEASES_AUTO``, по умолчанию выключена:
фонотека не должна расти без спроса. Включённая — новые треки уходят в
обычное «Артист целиком» через службу (``POST /api/artists/import``).

Запросы идут через ``similar._get``: не чаще одного в ``similar.MIN_INTERVAL``
(0,15 с — это 33 за пять секунд при лимите Deezer 50).
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from collections import Counter
from pathlib import Path

import httpx
from dotenv import dotenv_values

from . import config, discography, ingest, notify, runtime, similar

logger = logging.getLogger(__name__)

MAX_ARTISTS = 150
RECENT_DAYS = 60
KEEP = 40
MISS_TTL = 30 * 24 * 3600
SHOW_NAMES = 3


def _key(name: str) -> str:
    return " ".join(similar.primary(name).lower().split())


def load() -> dict:
    """Что лежит в файле. Нет файла или он битый — пусто: полка просто
    не покажется, а ночью файл соберётся заново."""
    try:
        state = json.loads(runtime.NEW_RELEASES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"artists": {}, "releases": []}
    if not isinstance(state, dict):
        return {"artists": {}, "releases": []}
    state.setdefault("artists", {})
    state.setdefault("releases", [])
    return state


def _save(state: dict) -> None:
    runtime.NEW_RELEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ingest.write_atomic(runtime.NEW_RELEASES_FILE, json.dumps(state, ensure_ascii=False).encode())


def library_artists(rows: list[dict], limit: int = MAX_ARTISTS) -> list[str]:
    """Артисты фонотеки по числу треков, первые ``limit``."""
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for row in rows:
        name = similar.primary(row.get("albumartist") or row.get("artist") or "").strip()
        if len(name) < 2:
            continue
        counts[_key(name)] += 1
        names.setdefault(_key(name), name)
    return [names[key] for key, _ in counts.most_common(limit)]


def _have(rows: list[dict]) -> dict[str, set[str]]:
    """Названия альбомов и треков фонотеки — по артисту."""
    have: dict[str, set[str]] = {}
    for row in rows:
        for who in {row.get("artist") or "", row.get("albumartist") or ""}:
            if not who:
                continue
            titles = have.setdefault(_key(who), set())
            for title in (row.get("album") or "", row.get("title") or ""):
                if title:
                    titles.add(discography._title_key(title))
    return have


def _is_had(release: dict, have: dict[str, set[str]]) -> bool:
    return discography._title_key(release["title"]) in have.get(_key(release["artist"]), set())


def check(rows: list[dict], now: float | None = None) -> dict:
    """Ночной проход. Возвращает, что нашлось впервые и сколько спрошено."""
    now = time.time() if now is None else now
    since = (datetime.datetime.fromtimestamp(now) - datetime.timedelta(days=RECENT_DAYS)).strftime(
        "%Y-%m-%d"
    )
    state = load()
    # Первый проход — точка отсчёта: всё за два месяца сразу было бы не
    # «новым», а дискографией. Полка покажет его, но без уведомления и
    # без автоскачивания.
    first = "checked_at" not in state
    known = state["artists"]
    have = _have(rows)
    old = {
        item["id"]: item for item in state["releases"] if isinstance(item, dict) and "id" in item
    }
    found: dict[str, dict] = {}
    asked = unknown = 0
    reachable = True

    with httpx.Client(timeout=similar.TIMEOUT) as client:
        for name in library_artists(rows):
            entry = known.get(_key(name))
            try:
                if entry is None or (not entry.get("id") and now - entry.get("at", 0) > MISS_TTL):
                    artist = discography.find_artist(name)
                    entry = {"id": artist["id"] if artist else None, "at": now}
                    known[_key(name)] = entry
                if not entry.get("id"):
                    unknown += 1
                    continue
                albums = similar._get(
                    client, discography.ALBUMS.format(id=entry["id"]), {"limit": 100}
                )
            except (similar._Unreachable, httpx.HTTPError) as exc:
                # Сеть пропала посреди прохода: что успели — сохраняем, а
                # прежние находки не трогаем — их не опровергли.
                logger.warning("Новые релизы: Deezer не отвечает (%s)", exc)
                reachable = False
                break
            asked += 1
            for album in albums:
                date = album.get("release_date") or ""
                title = (album.get("title") or "").strip()
                if (
                    album.get("record_type") not in discography.KINDS
                    or date < since
                    or not title
                    or discography.is_version(title)
                ):
                    continue
                release = {
                    "id": str(album.get("id")),
                    "artist": name,
                    "title": title,
                    "kind": album["record_type"],
                    "date": date,
                    "cover": album.get("cover_medium") or "",
                    "seen": old.get(str(album.get("id")), {}).get("seen", now),
                }
                if release["id"] in old:
                    release["queued"] = old[release["id"]].get("queued", False)
                elif first:
                    release["queued"] = True
                if not _is_had(release, have):
                    found[release["id"]] = release

    if not reachable:
        # Не дошли до всех — остальные прежние находки ещё в силе.
        for rid, item in old.items():
            if rid not in found and item.get("date", "") >= since and not _is_had(item, have):
                found[rid] = item

    releases = sorted(found.values(), key=lambda r: (r["date"], r["seen"]), reverse=True)[:KEEP]
    fresh = [] if first else [r for r in releases if r["id"] not in old]
    state.update(artists=known, releases=releases, checked_at=now)
    _save(state)
    return {"new": fresh, "asked": asked, "unknown": unknown, "complete": reachable}


def _service_token() -> str:
    """Токен службы. Не из config: export_shelves.py ставит заглушку до
    импорта config (наружу он не слушает), и её .env уже не перекрывает."""
    env = Path(config.__file__).with_name(".env")
    return str(dotenv_values(env).get("API_TOKEN") or "")


def _release_tracks(client: httpx.Client, release: dict) -> list[dict]:
    tracks = []
    for row in similar._get(client, discography.TRACKS.format(id=release["id"]), {"limit": 100}):
        title = (row.get("title") or "").strip()
        if not title or discography.is_version(title):
            continue
        artist = ((row.get("artist") or {}).get("name") or release["artist"]).strip()
        tracks.append(
            {
                "artist": artist,
                "title": title,
                "duration": row.get("duration") or None,
                "album": release["id"],
            }
        )
    return tracks


def queue_downloads() -> int:
    """Скачать новое само (``NEW_RELEASES_AUTO``). Возвращает, сколько
    релизов отдано службе.

    Через службу, а не записью в список «Артиста целиком» отсюда: список
    служба держит под своим замком, и две записи из двух процессов стёрли бы
    одна другую. Не дозвались — релиз остаётся неотданным до следующей ночи.
    """
    if not config.NEW_RELEASES_AUTO:
        return 0
    state = load()
    todo = [r for r in state["releases"] if not r.get("queued")]
    if not todo:
        return 0
    sent = 0
    try:
        with httpx.Client(timeout=similar.TIMEOUT) as client:
            for release in todo:
                tracks = _release_tracks(client, release)
                if tracks:
                    answer = client.post(
                        f"http://127.0.0.1:{config.PORT}/api/artists/import",
                        json={"tracks": tracks},
                        headers={"Authorization": f"Bearer {_service_token()}"},
                    )
                    answer.raise_for_status()
                release["queued"] = True
                sent += 1
    except (similar._Unreachable, httpx.HTTPError) as exc:
        logger.warning("Новые релизы: не отдал в скачивание (%s)", exc)
    _save(state)
    return sent


def shelf() -> list[dict]:
    """Для главной: без служебных полей."""
    return [
        {k: r.get(k) for k in ("id", "artist", "title", "kind", "date", "cover")}
        for r in load()["releases"]
    ]


def tell(fresh: list[dict]) -> None:
    """Уведомление о впервые увиденном. Синхронно: скрипт вот-вот выйдет,
    а фоновый поток notify.send умер бы вместе с ним."""
    if not fresh or not config.DESKTOP_NOTIFICATIONS:
        return
    names = [f"{r['artist']} — {r['title']}" for r in fresh[:SHOW_NAMES]]
    if len(fresh) > SHOW_NAMES:
        names.append(f"и ещё {len(fresh) - SHOW_NAMES}")
    notify._show(f"Новые релизы: {len(fresh)}", "\n".join(names), False)
