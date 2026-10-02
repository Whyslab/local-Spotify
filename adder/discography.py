"""Артист целиком: его альбомы, EP и синглы — списком, потом в загрузку.

Список берётся из Deezer: он знает дискографию этой фонотеки (русский рэп)
и длительность каждого трека, а по длительности на YouTube отличают студийную
запись от клипа и концертника. Сборники не берутся — почти всё в них
повторяет альбомы. Версии (ремиксы, sped up, slowed, инструменталы,
концертные и акустические) не предлагаются вовсе: решено 02.10.2026, у
синглов этой музыки их бывает треть.

Одна песня на сингле и потом на альбоме — один трек: берётся из альбома,
он полнее размечен. «Одна песня» — то же название и та же длительность
(±5 с): у Платины «Биг бой» на четыре минуты и «Биг бой» на 70 секунд —
разные треки, и по одному названию длинный прятался за коротким. Что уже
есть в фонотеке, показывается отмеченным «есть» и не качается.

Загрузка — не в запросе. Каждый трек ищется на YouTube отдельным вызовом
yt-dlp (секунды), и у артиста на полторы сотни треков запрос жил бы минуты.
Поэтому выбранное ложится в список ожидания (`IMPORT_FILE`), а один поток
по очереди находит трек и ставит его в обычную загрузку. Список на диске:
перезапуск службы посреди работы её не теряет.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from . import ingest, outside, runtime, similar

logger = logging.getLogger(__name__)

ARTIST = "https://api.deezer.com/artist/{id}"
ALBUMS = "https://api.deezer.com/artist/{id}/albums"
TRACKS = "https://api.deezer.com/album/{id}/tracks"

# Порядок важен: одна песня в нескольких релизах берётся из первого по нему.
KINDS = {"album": 0, "ep": 1, "single": 2}

# Слова версии. Ищутся только в скобках и после « - »: «Live Forever» —
# название, а «(Live Version)» — концерт.
_MARKERS = re.compile(
    r"\b(?:remix\w*|rmx|ремикс\w*|rework\w*|reflip|live|лайв|концерт\w*|concert|acoustic"
    r"|акусти\w*|instrumental\w*|инструментал\w*|минус|karaoke|караоке|sped|speed|slowed"
    r"|reverb|nightcore|8d|version|верси\w*)\b",
    re.IGNORECASE,
)
# А эти — версия, где бы ни стояли: «Кукла sped up», «VK Акустика».
_ANYWHERE = re.compile(
    r"\b(?:sped\s*up|speed\s*up|spedup|speedup|nightcore|slowed\s*(?:\+|and|&)\s*reverb"
    r"|vk\s+акустика)\b",
    re.IGNORECASE,
)
# Скобка с именами — не версия: «(prod. by Live)», «(feat. X)».
_CREDIT = re.compile(r"^\s*(?:feat|ft|prod|with|при\s+уч)", re.IGNORECASE)
# «Version» с этими словами — оригинал или его издание: «Album Version»,
# «Deluxe Version», «Remastered Version». Если у Deezer есть только
# делюкс-издание альбома, прятать его — потерять альбом.
_PLAIN_VERSION = re.compile(
    r"\b(?:album|original|radio|explicit|clean|deluxe|expanded|remaster\w*|anniversary)"
    r"\s+(?:version|edition)\b",
    re.IGNORECASE,
)


def _parts(title: str) -> list[tuple[str, bool]]:
    """Скобки названия и хвост после « - » (второе — «это хвост»)."""
    parts = [(p, False) for p in re.findall(r"[(\[]([^)\]]*)[)\]]", title)]
    dash = re.split(r"\s[-–—]\s", title, maxsplit=1)
    if len(dash) == 2:
        parts.append((dash[1], True))
    return parts


def is_version(title: str) -> bool:
    """Ремикс, ускоренная, концертная и прочие «не оригинал»."""
    text = title or ""
    if _ANYWHERE.search(text):
        return True
    for part, after_dash in _parts(text):
        if _CREDIT.match(part):
            continue
        words = {m.group(0).lower() for m in _MARKERS.finditer(part)}
        # «Плюс - Минус» — название, а «(минус)» — инструментал.
        if after_dash:
            words.discard("минус")
        if not words or (words == {"version"} and _PLAIN_VERSION.search(part)):
            continue
        return True
    return False


def _latin(text: str) -> str:
    """Латиницей и без знаков: Deezer пишет «Vozrast», теги — «Возраст»."""
    return " ".join(outside._norm(text).translate(outside._LATIN).split())


# Скобки, которые не делают трек другим: переиздание, бонус.
_EDITION = re.compile(r"\s*[(\[][^)\]]*\b(?:remaster\w*|bonus|бонус)\b[^)\]]*[)\]]", re.IGNORECASE)


def _title_key(title: str) -> str:
    """Название для сравнения: без «feat», переизданий и знаков, латиницей."""
    bare = _EDITION.sub("", outside.core_title(title or ""))
    return _latin(bare) or (title or "").strip().lower()


def _names(text: str) -> set[str]:
    """Все артисты строки целиком, латиницей: «Друг • Vozrast feat. X».

    Целиком, а не подстрокой: «DK» не должен находиться внутри «madk1d».
    """
    pieces = re.split(r"\s*(?:•|&|,|;|/|\bfeat\.?|\bft\.?|\bx\b)\s*", text or "", flags=re.I)
    return {name for name in (_latin(p) for p in pieces) if name}


def _close(a: float | None, b: float | None, within: float) -> bool:
    """Длительности совпадают — или одной из них нет, и судить не по чему."""
    return not a or not b or abs(float(a) - float(b)) <= within


# Одна песня в двух релизах — длительность в пределах этого (секунды).
SAME_SONG = 5


def _song_key(artist: str, title: str) -> tuple[str, str]:
    """Ключ ожидания: первый артист и название."""
    return _latin(similar.primary(artist)), _title_key(title)


# ---------------------------------------------------------------------------
# Что есть у артиста
# ---------------------------------------------------------------------------


def search_artists(query: str, limit: int = 8) -> list[dict]:
    """Артисты Deezer по имени — выбрать нужного, а не тёзку."""
    query = (query or "").strip()
    if not query:
        return []
    with httpx.Client(timeout=similar.TIMEOUT) as client:
        rows = similar._get(client, similar.SEARCH, {"q": query, "limit": max(1, min(limit, 25))})
    return [
        {
            "id": str(row["id"]),
            "name": row.get("name") or "",
            "picture": row.get("picture_medium") or "",
            "fans": row.get("nb_fan") or 0,
            "albums": row.get("nb_album") or 0,
        }
        for row in rows
        if row.get("id")
    ]


def find_artist(name: str) -> dict | None:
    """Тот же артист, что в фонотеке: по точному имени, иначе латиницей.

    Точная запись без единого релиза — пустышка Deezer: «Алина Орлова»
    кириллицей пуста, а её песни у него под «Alina Orlova». Такую не берём.
    """
    wanted = similar.primary(name)
    latin = outside._latin_words(wanted)
    found = search_artists(wanted, limit=10)
    same = [a for a in found if a["name"].strip().lower() == wanted.lower() and a["albums"]] or [
        a for a in found if latin and outside._latin_words(a["name"]) == latin and a["albums"]
    ]
    return max(same, key=lambda a: a["fans"]) if same else None


def _about(client: httpx.Client, artist_id: str) -> dict:
    """Сам артист: имя. Неизвестный — LookupError, сбой — _Unreachable."""
    response = client.get(ARTIST.format(id=artist_id))
    if not response.is_success:
        raise similar._Unreachable(f"ответ {response.status_code}")
    try:
        about = response.json() or {}
    except ValueError as exc:
        raise similar._Unreachable("ответ не JSON") from exc
    if "error" in about:
        raise LookupError("Deezer не знает такого артиста")
    return about


def discography(artist_id: str, library_rows: list[dict]) -> dict:
    """Альбомы, EP и синглы артиста с треками — для списка с галочками.

    У трека: `have` — уже в фонотеке. Версии и повторы песен сюда не
    попадают, их число — в `hidden`.
    """
    with httpx.Client(timeout=similar.TIMEOUT) as client:
        about = _about(client, artist_id)
        # «Уже есть» — та же песня у того же первого артиста, или то же
        # название у трека фонотеки, где этот артист хоть где-то в списке
        # (фит, альбомный артист): трек с его альбома, выпущенный другом,
        # лежит в фонотеке под другом.
        own = _latin(about.get("name") or "")
        library = _library_songs(library_rows)
        releases = [
            r
            for r in similar._get(client, ALBUMS.format(id=artist_id), {"limit": 500})
            if r.get("record_type") in KINDS
        ]
        # Сначала альбомы, потом EP, потом синглы; внутри — по дате выхода:
        # песня берётся из первого релиза, где она вышла как оригинал.
        releases.sort(key=lambda r: (KINDS[r["record_type"]], r.get("release_date") or ""))

        seen: list[tuple[str, str, float | None]] = []
        hidden = hidden_releases = 0
        out = []
        for release in releases:
            if is_version(release.get("title") or ""):
                hidden_releases += 1
                continue
            tracks = []
            for row in similar._get(client, TRACKS.format(id=release["id"]), {"limit": 500}):
                title = (row.get("title") or "").strip()
                artist = ((row.get("artist") or {}).get("name") or about.get("name") or "").strip()
                duration = row.get("duration") or None
                if not title or is_version(title):
                    hidden += 1
                    continue
                me = (_latin(similar.primary(artist)), _title_key(title), duration)
                if any(_repeat(me, other) for other in seen):
                    hidden += 1
                    continue
                seen.append(me)
                tracks.append({"artist": artist, "title": title, "duration": duration, "me": me})
            if tracks:
                out.append(
                    {
                        "id": str(release["id"]),
                        "title": release.get("title") or "",
                        "kind": release["record_type"],
                        "year": (release.get("release_date") or "")[:4],
                        "cover": release.get("cover_medium") or "",
                        "tracks": tracks,
                    }
                )
    _mark_have(out, own, library)
    return {
        "artist": {"id": str(about.get("id")), "name": about.get("name") or ""},
        "releases": out,
        "hidden": hidden,
        "hidden_releases": hidden_releases,
    }


def _repeat(a: tuple[str, str, float | None], b: tuple[str, str, float | None]) -> bool:
    """Та же песня второй раз: то же название и та же длина.

    Артист может отличаться — совместный трек Deezer записывает то на одного,
    то на другого («Ковры» Скриптонита и TAYOKA, 223 с у обоих); тогда
    длительность должна совпасть почти точно.
    """
    if a[1] != b[1]:
        return False
    if a[0] == b[0]:
        return _close(a[2], b[2], SAME_SONG)
    return bool(a[2] and b[2]) and _close(a[2], b[2], 2)


def _library_songs(rows: list[dict]) -> list[tuple[set[str], set[str], float | None]]:
    """Фонотека для «есть»: (все артисты, варианты названия, длительность).

    Название бывает с артистом впереди — «GONE.Fludd feat. IROH — ВКУС ЯДА»:
    вариант без него тоже в счёт.
    """
    songs = []
    for row in rows:
        names = _names(f"{row.get('artist') or ''} • {row.get('albumartist') or ''}")
        title = row.get("title") or ""
        titles = {_title_key(title)}
        parts = re.split(r"\s[-–—]\s", title, maxsplit=1)
        if len(parts) == 2:
            # Длительность всё равно должна совпасть — «Мой Дилер -
            # Инопланетянин» не станет чужой песней «Инопланетянин».
            titles.add(_title_key(parts[1]))
        songs.append((names, titles, row.get("duration")))
    return songs


def _mark_have(
    releases: list[dict], own: str, library: list[tuple[set[str], set[str], float | None]]
) -> None:
    """Пометить «есть». Достаточно названия и артиста (целиком, а не куском):
    в фонотеке часто версия из клипа, на минуту длиннее студийной, — по
    длительности она считалась бы отсутствующей и скачалась второй раз.
    Длительность решает, только когда у артиста несколько разных песен с
    одним названием («Биг бой» на 70 и на 260 с): «есть» — ближайшая по
    длине к файлу фонотеки."""
    tracks = [t for r in releases for t in r["tracks"]]
    for track in tracks:
        track["have"] = False
    for names, titles, length in library:
        same = [t for t in tracks if t["me"][1] in titles and (t["me"][0] in names or own in names)]
        if not same:
            continue
        if length and len(same) > 1:
            same = [min(same, key=lambda t: abs(float(t["duration"] or length) - float(length)))]
        for track in same:
            track["have"] = True
    for track in tracks:
        del track["me"]


# ---------------------------------------------------------------------------
# Загрузка выбранного
# ---------------------------------------------------------------------------
#
# Пункт списка: artist, title, duration, album (id Deezer), state —
# "wait" → "queued" (поставлен в загрузку) / "had" (такая задача уже была) /
# "missed" (на YouTube нет той же записи, или поиск так и не удался).
# У ждущего могут быть attempts, retry_at и error — после неудачного поиска.


class TryLater(Exception):
    """Поиск не удался (сеть, ограничение YouTube): «не дозвались», а не
    «такой записи нет». Пункт ждёт и пробуется снова позже."""


_lock = threading.Lock()
_wake = threading.Event()
_thread: threading.Thread | None = None
# Как поставить один трек: "queued", "had" или "missed"; TryLater — повторить
# позже. Даёт app.py — там очередь задач и строгий поиск на YouTube.
QueueOne = Callable[[dict], str]

# Через сколько повторить неудачный поиск и сколько раз пробовать. Пункт,
# который не ищется, не держит остальных: пока он ждёт, идут следующие.
RETRY_AFTER = 300
MAX_ATTEMPTS = 5


def _file() -> Path:
    return runtime.ARTIST_IMPORT_FILE


def _load() -> list[dict]:
    """Список с диска. Нет файла — пусто; битый файл откладывается в
    сторону (а не молча считается пустым — иначе следующая запись стёрла бы
    его насовсем)."""
    try:
        text = _file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("не список")
    except ValueError as exc:
        aside = _file().with_name(f"{_file().name}.broken-{int(time.time())}")
        _file().rename(aside)
        logger.warning(
            "Артист целиком: список повреждён (%s), отложен в %s",
            exc,
            aside.name,
            extra={"task_id": "system"},
        )
        return []
    return [i for i in items if isinstance(i, dict) and i.get("artist") and i.get("title")]


def _save(items: list[dict]) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    ingest.write_atomic(_file(), json.dumps(items, ensure_ascii=False).encode())


def add(tracks: list[dict]) -> int:
    """Положить выбранное в ожидание. Возвращает, сколько новых пунктов."""
    with _lock:
        items = _load()
        waiting = {_song_key(i["artist"], i["title"]) for i in items if i.get("state") == "wait"}
        added = 0
        for track in tracks:
            artist = str(track.get("artist") or "").strip()
            title = str(track.get("title") or "").strip()
            if not artist or not title:
                continue
            key = _song_key(artist, title)
            if key in waiting:
                continue
            waiting.add(key)
            duration = track.get("duration")
            items.append(
                {
                    "artist": artist,
                    "title": title,
                    "duration": float(duration) if isinstance(duration, int | float) else None,
                    "album": str(track.get("album") or ""),
                    "state": "wait",
                }
            )
            added += 1
        _save(items)
    _wake.set()
    return added


def status() -> dict:
    """Сколько ждёт, сколько поставлено, чего не нашлось."""
    with _lock:
        items = _load()
    counts = {"wait": 0, "queued": 0, "had": 0, "missed": 0}
    for item in items:
        state = item.get("state", "wait")
        counts[state] = counts.get(state, 0) + 1
    failing = [i for i in items if i.get("state") == "wait" and i.get("attempts")]
    return {
        **counts,
        "total": len(items),
        # Ждут повтора после неудачного поиска — и почему.
        "failing": len(failing),
        "last_error": failing[-1].get("error", "") if failing else "",
        "missed_tracks": [
            {"artist": i["artist"], "title": i["title"]}
            for i in items
            if i.get("state") == "missed"
        ],
    }


def clear_finished() -> None:
    """Убрать из списка всё, кроме ждущего: прошлые итоги больше не нужны."""
    with _lock:
        _save([i for i in _load() if i.get("state") == "wait"])


def cancel_waiting() -> int:
    """Не искать оставшееся. Возвращает, сколько пунктов снято."""
    with _lock:
        items = _load()
        kept = [i for i in items if i.get("state") != "wait"]
        _save(kept)
    return len(items) - len(kept)


def _next(now: float) -> tuple[dict | None, float | None]:
    """Первый ждущий, чей повтор уже настал, — и когда настанет ближайший."""
    with _lock:
        items = _load()
    later = None
    for item in items:
        if item.get("state") != "wait":
            continue
        at = float(item.get("retry_at") or 0)
        if at <= now:
            return item, None
        later = at if later is None else min(later, at)
    return None, later


def _update(item: dict, **fields) -> None:
    with _lock:
        items = _load()
        key = _song_key(item["artist"], item["title"])
        for current in items:
            if (
                current.get("state") == "wait"
                and _song_key(current["artist"], current["title"]) == key
            ):
                current.update(fields)
                break
        _save(items)


def step(queue_one: QueueOne) -> float | None:
    """Разобрать один ждущий пункт.

    Возвращает, сколько секунд можно спать: 0 — разобран пункт, дальше
    сразу; число — все ждущие отложены до повтора; None — ждущих нет.
    """
    now = time.time()
    item, later = _next(now)
    if item is None:
        return None if later is None else max(1.0, later - now)
    try:
        state = queue_one(item)
    except runtime.ShutdownRequested:
        raise
    except TryLater as exc:
        attempts = int(item.get("attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            _update(item, state="missed", attempts=attempts, error=str(exc)[:200])
        else:
            _update(item, attempts=attempts, retry_at=now + RETRY_AFTER, error=str(exc)[:200])
        return 0
    except Exception as exc:  # один сбой не должен останавливать остальных
        logger.warning(
            "Артист целиком: %s — %s: %s",
            item["artist"],
            item["title"],
            exc,
            extra={"task_id": "system"},
        )
        state = "missed"
    _update(item, state=state)
    return 0


def _work(queue_one: QueueOne) -> None:
    while not runtime.shutdown_event.is_set():
        try:
            pause = step(queue_one)
        except runtime.ShutdownRequested:
            return
        except Exception as exc:  # диск, битый пункт — поток не должен умереть
            logger.warning("Артист целиком: %s", exc, extra={"task_id": "system"})
            pause = 60.0
        if pause == 0:
            continue
        _wake.wait(timeout=min(pause or 30.0, 30.0))
        _wake.clear()


def start(queue_one: QueueOne) -> None:
    """Поток, который разбирает ожидание; после перезапуска — с того же места."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_work, args=(queue_one,), name="artist-import", daemon=True)
    _thread.start()
