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
import threading
import time
from pathlib import Path

import requests

from . import library, runtime

logger = logging.getLogger(__name__)

CACHE_DIR = runtime.PROJECT / "lyrics-cache"
API = "https://lrclib.net/api/get"
SEARCH_API = "https://lrclib.net/api/search"
# Версия правил поиска. Промах, записанный по старым правилам, перепроверяется
# сразу, не дожидаясь двух недель: новые правила находят то, что старые не
# могли.
RULES = 2
# Насколько длительность в каталоге может разойтись с файлом. Текст с
# таймингами от другой версии трека (радио-edit, клип со вставкой) побежит
# мимо музыки, поэтому допуск небольшой.
DURATION_TOLERANCE = 4
# Пауза между треками при обходе фонотеки: каталог чужой и бесплатный.
BACKFILL_PAUSE = 1.0
BACKFILL_EVERY = 24 * 3600
# Обход, прерванный отказами каталога, продолжается через час, а не через сутки.
BACKFILL_RETRY = 3600
# Сколько отказов подряд терпит обход, прежде чем отложить себя.
BACKFILL_GIVE_UP = 8
# Между любыми двумя запросами к каталогу — не меньше этого. На промахе трек
# стоит до пяти запросов подряд, и без паузы между ними LRCLIB отвечал 503
# уже на первых треках обхода.
REQUEST_INTERVAL = 0.5
_pace_lock = threading.Lock()
_last_request = 0.0
# Сколько каталог просил подождать в последнем отказе (Retry-After), секунд.
last_retry_after: float | None = None
TIMEOUT = 8
MISS_TTL = 14 * 24 * 3600  # промах перепроверяем через две недели
AGENT = "local-Spotify (personal music library, https://github.com/Whyslab/local-Spotify)"

# [mm:ss.xx] или [mm:ss] в начале строки
_STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LEADING_STAMPS = re.compile(r"^(?:\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+")
# Служебный тег LRC: [ar:Артист], [ti:Название], [length:03:07]
_LRC_TAG = re.compile(r"^\[[a-z]+:[^\]]*\]$", re.IGNORECASE)


def _get(url: str, params: dict | None = None) -> requests.Response:
    """Запрос к каталогу с общим темпом для всех потоков: обход, плеер и
    ручной поиск делят одну квоту. Отказ 503 запоминает Retry-After."""
    global _last_request, last_retry_after
    with _pace_lock:
        wait = _last_request + REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    response = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": AGENT})
    if response.status_code in (429, 503):
        try:
            last_retry_after = float(response.headers.get("Retry-After", ""))
        except ValueError:
            last_retry_after = None
    else:
        # Просьба подождать относится к тому отказу, а не ко всем следующим.
        last_retry_after = None
    return response


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


# Хвосты названий, которые пишет YouTube, а не автор: «(Lyrics)», «(Audio)»,
# «[Official Video]», «(prod. …)», «(feat. …)». Каталог о них не знает.
_JUNK = re.compile(
    r"\s*[(\[][^)\]]*\b(lyrics|audio|official|video|clip|клип|текст|visuali[sz]er"
    r"|prod|feat|ft|speed ?up|sped ?up|slowed)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
        "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
    }
)  # fmt: skip


def _words(text: str) -> set[str]:
    """Слова латиницей: «Гио Пика» и «Gio Pika» — одно и то же."""
    text = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return set(text.translate(_LATIN).split())


def title_variants(title: str, artist: str = "") -> list[str]:
    """Как ещё может называться трек в каталоге.

    В тегах фонотеки бывает «GONE.Fludd, ЛСП — Ути-Пути»: артист, попавший в
    название из имени ролика. Тогда пробуется и то, что после тире — но
    только если до тире стоит артист этого трека. «Козловский - Remix» или
    «worry - Slowed» — это не артист и песня, и «Remix» одним словом совпал
    бы с любым ремиксом того же артиста.
    """
    base = _JUNK.sub("", clean_title(title)).strip() or (title or "").strip()
    variants = [base]
    for dash in (" — ", " – ", " - "):
        if dash in base:
            head, tail = (part.strip() for part in base.split(dash, 1))
            head_words = _words(head)
            names = [_words(name) for name in artist_names(artist)]
            if tail and any(name and name <= head_words for name in names):
                variants.append(tail)
            break
    return list(dict.fromkeys(variants))


def artist_names(artist: str) -> list[str]:
    """Все артисты строки: «A • B», «A & B», «A feat. B»."""
    parts = re.split(r"\s*(?:•|&|,| feat\.? | ft\.? | x )\s*", artist or "", flags=re.IGNORECASE)
    return [part.strip() for part in parts if part.strip()]


def _same_artist(ours: str, theirs: str) -> bool:
    their_words = _words(theirs)
    return any(_words(name) and _words(name) <= their_words for name in artist_names(ours))


def _pick(results: list, artist: str, title: str, duration: float | None) -> dict | None:
    """Подходящая запись из выдачи поиска, или None.

    Три условия сразу: тот же артист, то же название и та же длина. Без
    артиста поиск по одному названию находил чужие песни с тем же именем
    («Break Up» у U-KISS вместо Baby Cute).
    """
    wanted = _words(title)
    for item in results or []:
        if not isinstance(item, dict):
            continue
        # Только записи с текстом. «Инструментал» из поиска — это чаще всего
        # чужой минус с тем же названием, и он спрятал бы настоящий текст.
        if not (item.get("syncedLyrics") or item.get("plainLyrics")):
            continue
        if duration and item.get("duration"):
            try:
                off = abs(float(item["duration"]) - float(duration))
            except (TypeError, ValueError):
                continue
            if off > DURATION_TOLERANCE:
                continue
        elif duration:
            continue
        if not _same_artist(artist, item.get("artistName") or ""):
            continue
        theirs = _words(_JUNK.sub("", item.get("trackName") or ""))
        # Пустое название совпало бы с чем угодно; короткое чужое — только
        # если покрывает хотя бы половину слов нашего.
        if not wanted or not theirs:
            continue
        if not (wanted <= theirs or (theirs <= wanted and 2 * len(theirs) >= len(wanted))):
            continue
        return item
    return None


def _search(artist: str, title: str, duration: float | None) -> dict | None:
    """Поиск по каталогу, когда точный запрос промахнулся.

    None — «каталог не ответил», {} — «не нашлось».
    """
    queried = False
    for variant in title_variants(title, artist):
        for params in (
            {"q": f"{primary_artist(artist)} {variant}"},
            {"track_name": variant},
        ):
            try:
                response = _get(SEARCH_API, params)
            except requests.RequestException as exc:
                logger.info("lyrics search: %s — %s", title, exc)
                return None
            if not response.ok:
                logger.info("lyrics search: %s — ответ %s", title, response.status_code)
                return None
            queried = True
            try:
                found = _pick(response.json(), artist, variant, duration)
            except ValueError:
                return None
            if found:
                return {**found, "via": "search"}
    return {} if queried else None


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
        line = raw[stamps[-1].end() :].strip()
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
        response = _get(API, params)
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
            fresh = cached.get("found") or (
                cached.get("rules", 1) >= RULES and (time.time() - cached.get("at", 0)) < MISS_TTL
            )
            if fresh:
                return cached
        except (ValueError, OSError):
            pass  # битый кэш — просто спросим заново

    if row is None:
        row = next((r for r in library.library_index() if r["path"] == rel_path), None)
    if row is None:
        return {"found": False, "reason": "Трека нет в фонотеке"}

    data = _ask(row["artist"], row["title"], row.get("album") or "", row.get("duration"))
    missed = data is not None and not (
        data.get("syncedLyrics") or data.get("plainLyrics") or data.get("instrumental")
    )
    if missed:
        # Точный запрос промахнулся — тегами этой фонотеки это обычное дело:
        # хвосты из YouTube, артист в названии, фиты. Ищем шире.
        data = _search(row["artist"], row["title"], row.get("duration"))
    if data is None:
        # Сеть не ответила. Не запоминаем: это не про этот трек.
        return {"found": False, "reason": "Каталог текстов не ответил"}

    return _store(rel_path, _result(data, row, "lrclib"))


def _result(data: dict, row: dict, source: str) -> dict:
    synced = parse_synced(data.get("syncedLyrics") or "")
    plain = (data.get("plainLyrics") or "").strip()
    result = {
        "found": bool(synced or plain),
        "at": time.time(),
        "rules": RULES,
        "synced": synced,
        "plain": plain,
        "source": source if (synced or plain) else "",
        "title": row["title"],
        "artist": row["artist"],
    }
    if data.get("instrumental") and not (synced or plain):
        result["instrumental"] = True
        result["reason"] = "Инструментал — слов нет"
    if result["found"]:
        # Откуда взят текст: чтобы найденное поиском можно было проверить и,
        # если правила поменяются, перепроверить только его.
        result["via"] = data.get("via") or "exact"
        result["matched"] = {
            "id": data.get("id"),
            "artist": data.get("artistName"),
            "title": data.get("trackName"),
            "duration": data.get("duration"),
        }
    return result


_store_lock = threading.Lock()


def _store(rel_path: str, result: dict) -> dict:
    with _store_lock:
        if not result.get("chosen"):
            # Выбранное руками главнее: поиск, начатый до выбора (обход, плеер,
            # новый трек), мог закончиться после него и затереть выбор.
            try:
                existing = json.loads(_key(rel_path).read_text(encoding="utf-8"))
                if existing.get("chosen"):
                    return existing
            except (OSError, ValueError):
                pass
        return _write(rel_path, result)


def _write(rel_path: str, result: dict) -> dict:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target = _key(rel_path)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        logger.warning("lyrics: не смог записать кэш: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Ручной выбор: когда сам поиск не нашёл или нашёл не то
# ---------------------------------------------------------------------------

MAX_CUSTOM_TEXT = 20_000


def _preview(item: dict) -> str:
    text = item.get("plainLyrics") or ""
    if not text and item.get("syncedLyrics"):
        timed = parse_synced(item["syncedLyrics"])
        text = "\n".join(line["line"] for line in timed if line["line"])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines[:2])[:160]


def candidates(row: dict, query: str = "") -> list[dict] | None:
    """Варианты из каталога для выбора руками. None — каталог не ответил.

    Правила здесь мягче, чем у автоматического поиска: выбирает человек, и
    чужая песня с тем же названием ему видна сразу. Подходящие по артисту,
    названию и длине стоят первыми и помечены.
    """
    if query.strip():
        requests_params = [{"q": query.strip()}]
    else:
        requests_params = []
        for variant in title_variants(row["title"], row["artist"]):
            requests_params.append({"q": f"{primary_artist(row['artist'])} {variant}"})
            requests_params.append({"track_name": variant})

    seen: set = set()
    found: list[dict] = []
    answered = False
    for params in requests_params:
        try:
            response = _get(SEARCH_API, params)
        except requests.RequestException as exc:
            logger.info("lyrics candidates: %s", exc)
            continue
        if not response.ok:
            logger.info("lyrics candidates: ответ %s", response.status_code)
            continue
        answered = True
        try:
            items = response.json()
        except ValueError:
            continue
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("id") is None or item["id"] in seen:
                continue
            if not (item.get("syncedLyrics") or item.get("plainLyrics")):
                continue
            seen.add(item.get("id"))
            found.append(item)
    if not answered:
        return None

    duration = row.get("duration")
    variants = title_variants(row["title"], row["artist"])

    def fits(item: dict) -> bool:
        return any(_pick([item], row["artist"], v, duration) for v in variants)

    def distance(item: dict) -> float:
        try:
            return abs(float(item["duration"]) - float(duration))
        except (KeyError, TypeError, ValueError):
            return 1e9

    found.sort(key=lambda item: (not fits(item), distance(item)))
    return [
        {
            "id": item["id"],
            "artist": item.get("artistName") or "",
            "title": item.get("trackName") or "",
            "album": item.get("albumName") or "",
            "duration": item.get("duration"),
            "synced": bool(item.get("syncedLyrics")),
            "preview": _preview(item),
            "fits": fits(item),
        }
        for item in found[:12]
    ]


def choose(rel_path: str, row: dict, record_id: int) -> dict | None:
    """Взять запись каталога, выбранную руками. None — каталог не ответил."""
    try:
        response = _get(f"https://lrclib.net/api/get/{int(record_id)}")
    except requests.RequestException as exc:
        logger.info("lyrics choose: %s", exc)
        return None
    if not response.ok:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    result = _result(data, row, "lrclib")
    if not result["found"]:
        return result
    # Выбрано руками — обход фонотеки это не перезапишет: он спрашивает
    # только треки без найденного текста, а _store не пишет поверх выбора.
    result["chosen"] = True
    result["via"] = "chosen"
    return _store(rel_path, result)


def save_custom(rel_path: str, row: dict, text: str) -> dict:
    """Свой текст. С метками [мм:сс] — с таймингами, без них — просто текст."""
    text = (text or "").strip()[:MAX_CUSTOM_TEXT]
    # Служебные строки LRC — [ar:…], [ti:…], [length:…] — не текст песни.
    lines = [line for line in text.splitlines() if not _LRC_TAG.match(line.strip())]
    # Метки времени — только в начале строки: «[10:30]» посреди строки — слова.
    timed = [line for line in lines if _STAMP.match(line.strip())]
    synced = parse_synced("\n".join(timed)) if len(timed) >= 3 else []
    plain = "\n".join(_LEADING_STAMPS.sub("", line).strip() for line in lines).strip()
    if not plain:
        raise ValueError("в тексте нет слов")
    result = _result({"syncedLyrics": "", "plainLyrics": plain}, row, "manual")
    result["synced"] = synced
    result["chosen"] = True
    result["via"] = "manual"
    return _store(rel_path, result)


# ---------------------------------------------------------------------------
# Обход фонотеки: текст ищется заранее, а не в момент включения
# ---------------------------------------------------------------------------


def needs_lookup(rel_path: str, now: float | None = None) -> bool:
    """Нет свежего ответа в кэше — трек надо спросить."""
    path = _key(rel_path)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if cached.get("found"):
        return False
    now = time.time() if now is None else now
    return cached.get("rules", 1) < RULES or now - cached.get("at", 0) >= MISS_TTL


BACKOFF = (30, 60, 120, 300, 600)


def backfill(
    stop: threading.Event | None = None,
    pause: float = BACKFILL_PAUSE,
    backoff: tuple[float, ...] = BACKOFF,
) -> dict:
    """Спросить текст у каждого трека, для которого нет свежего ответа.

    Так текст есть у трека до первого включения, промахи перепроверяются
    раз в две недели, а трек, который сам по себе не играли, тоже не остаётся
    без текста. Идёт медленно, по треку в секунду: каталог чужой.

    Отказ каталога («слишком часто») — повод подождать, а не бросить: пауза
    растёт с каждым отказом подряд (или столько, сколько каталог попросил), и
    только после ``BACKFILL_GIVE_UP`` отказов подряд обход откладывается.
    ``complete`` в ответе — дошёл ли обход до конца фонотеки.
    """
    stats = {"asked": 0, "found": 0, "missed": 0, "unreachable": 0, "errors": 0, "complete": False}
    in_a_row = 0

    def rest(seconds: float) -> bool:
        """Подождать. True — службу останавливают, пора выходить."""
        if stop is not None:
            return stop.wait(seconds)
        time.sleep(seconds)
        return False

    for row in library.library_index():
        if stop is not None and stop.is_set():
            return stats
        if not needs_lookup(row["path"]):
            continue
        try:
            result = for_track(row["path"], row=row)
        except Exception as exc:  # noqa: BLE001 — один странный ответ не должен остановить обход
            logger.info("Тексты: %s — %s", row["path"], exc)
            stats["errors"] += 1
            continue
        stats["asked"] += 1
        if result.get("reason") == "Каталог текстов не ответил":
            stats["unreachable"] += 1
            in_a_row += 1
            if in_a_row >= BACKFILL_GIVE_UP:
                return stats
            # Не меньше своей ступени: LRCLIB присылает Retry-After в секунду,
            # и с ним пауза после отказа не росла вовсе.
            delay = max(last_retry_after or 0, backoff[min(in_a_row, len(backoff)) - 1])
            if rest(delay):
                return stats
            continue
        in_a_row = 0
        stats["found" if result.get("found") else "missed"] += 1
        if rest(pause):
            return stats
    # С отказами обход не полный: отказанные треки спросим через час, а не
    # через сутки.
    stats["complete"] = stats["unreachable"] == 0
    return stats


def start_backfill() -> threading.Thread:
    """Обход в фоне: через минуту после старта службы, потом раз в сутки.

    Прерванный отказами каталога — через час: иначе тысяча треков проходилась
    бы неделями.
    """

    def loop() -> None:
        if runtime.shutdown_event.wait(60):
            return
        while not runtime.shutdown_event.is_set():
            complete = False
            try:
                stats = backfill(stop=runtime.shutdown_event)
                complete = stats["complete"]
                if stats["asked"] or stats["errors"]:
                    logger.info("Тексты: обход фонотеки — %s", stats)
            except Exception:  # noqa: BLE001 — обход не должен ронять службу
                logger.exception("Тексты: обход фонотеки упал")
            if runtime.shutdown_event.wait(BACKFILL_EVERY if complete else BACKFILL_RETRY):
                return

    thread = threading.Thread(target=loop, name="lyrics-backfill", daemon=True)
    thread.start()
    return thread
