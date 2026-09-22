"""Треки не из фонотеки — для умного перемешивания «как в Spotify».

Умное перемешивание раньше подмешивало только своё: треки фонотеки, которых
нет в подборке. Просили другого — чтобы в очереди звучало и то, чего в
фонотеке нет вовсе, но что подходит. Решения, принятые 22.09.2026:

* **Доля — примерно один трек из трёх.** Как у Spotify: своё узнаётся, новое
  заметно.
* **Скачанное лежит во временном кэше**, а не в фонотеке. Живёт 30 дней после
  последнего прослушивания и удаляется само. Понравилось — кнопка «в фонотеку»
  отправляет файл обычным путём импорта, с тегами, обложкой и Navidrome.
  Иначе фонотека росла бы сама, и ненужное пришлось бы вычищать руками.

Что подходит, решает Deezer: похожие артисты и их популярные треки (см.
``similar``). Версию на YouTube выбирает длительность из Deezer: клип со
вставками, концерт и кавер на пять секунд не попадают, а автовыбор без этой
проверки однажды уже набил фонотеку концертниками.

Скачивание идёт в фоне, заранее: плеер просит следующие два трека, пока
играет текущий. Трек, который не успел или не смог скачаться, плеер
пропускает — очередь не встаёт.

Путь такого трека в очереди — ``outside:<ключ>``. Ключ — 16 шестнадцатеричных
знаков, и ничего другого после приставки не принимается: путь приходит из
сети, и превратить его в файл вне кэша должно быть нельзя.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import httpx

from . import ingest, runtime, similar

logger = logging.getLogger(__name__)

PREFIX = "outside:"
# Доля очереди, отданная трекам не из фонотеки.
SHARE = 1 / 3

KEEP_SECONDS = 30 * 24 * 3600
# Неудача запоминается на неделю: ролик с проверкой возраста завтра не
# станет доступнее, а дёргать YouTube на каждом перемешивании незачем.
FAILED_SECONDS = 7 * 24 * 3600
# Кандидат, которого так и не попросили скачать, через неделю забывается.
# Не раньше: очередь, открытая на телефоне, может ждать своего часа днями, а
# забытый кандидат скачать уже нельзя — плеер его только пропустит.
WANTED_SECONDS = 7 * 24 * 3600

# Насколько длительность ролика может разойтись с Deezer. Тишина в начале и
# конце даёт секунду-две; клип со вставкой или концерт — десятки.
DURATION_TOLERANCE = 5
SEARCH_RESULTS = 8

# Сколько артистов очереди спрашивать о похожих и сколько похожих брать у
# каждого. Ответы кэшируются навсегда, так что дорого только в первый раз.
SEED_LIMIT = 12
NEIGHBOURS_PER_SEED = 6
# Сколько секунд перемешивание готово ждать Deezer. Что не успело, досчитается
# в фоне и попадёт в кэш — к следующему разу.
CANDIDATE_BUDGET = 6.0

_KEY = re.compile(r"^[0-9a-f]{16}$")

# Слова, которые выдают не ту версию. Проверяются по названию ролика, но
# только если их нет в названии самого трека: «Live Forever» — не концерт.
_UNWANTED = re.compile(
    r"\b(live|concert|концерт|karaoke|караоке|cover|кавер|react|reacts|reaction|reacting|реакция"
    r"|remix|ремикс|sped up|speed up|slowed|nightcore|8d|instrumental|минус"
    r"|bass boosted|разбор|tutorial|how to play|mashup|мэшап)\b"
)


def _dir() -> Path:
    return runtime.OUTSIDE_DIR


def key_for(artist: str, title: str) -> str:
    who = similar.primary(artist).strip().lower()
    what = (title or "").strip().lower()
    return hashlib.sha1(f"{who}|{what}".encode()).hexdigest()[:16]


def track_key(path: str) -> str | None:
    """Ключ из пути очереди, или None, если это не трек со стороны."""
    if not path or not path.startswith(PREFIX):
        return None
    key = path[len(PREFIX) :]
    return key if _KEY.fullmatch(key) else None


def is_outside(path: str) -> bool:
    return bool(path) and path.startswith(PREFIX)


# ---------------------------------------------------------------------------
# Кэш на диске: <ключ>.json — что это и в каком состоянии, <ключ>.m4a — звук
# ---------------------------------------------------------------------------

_META_LOCK = threading.Lock()


def _meta_path(key: str) -> Path:
    return _dir() / f"{key}.json"


def _audio_path(key: str) -> Path:
    return _dir() / f"{key}.m4a"


def read_meta(key: str) -> dict | None:
    try:
        return json.loads(_meta_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta(key: str, meta: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    target = _meta_path(key)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def _update_meta(key: str, **fields) -> dict:
    with _META_LOCK:
        meta = read_meta(key) or {}
        meta.update(fields)
        _write_meta(key, meta)
        return meta


def remember(found: list[dict]) -> None:
    """Записать кандидатов, чтобы их можно было попросить скачать.

    Уже известных не трогает: скачанный трек не должен снова стать «нужным»,
    а неудачный — попытаться ещё раз раньше срока.
    """
    now = time.time()
    with _META_LOCK:
        for item in found:
            if read_meta(item["key"]) is not None:
                continue
            _write_meta(
                item["key"],
                {
                    "artist": item["artist"],
                    "title": item["title"],
                    "duration": item.get("duration"),
                    "album": item.get("album") or "",
                    "cover": item.get("cover") or "",
                    "status": "wanted",
                    "at": now,
                },
            )


def status(key: str) -> str:
    """wanted | pending | ready | failed | unknown."""
    meta = read_meta(key)
    if meta is None:
        return "unknown"
    state = meta.get("status") or "wanted"
    if state == "ready" and not _audio_path(key).is_file():
        return "wanted"
    if state != "ready" and key in _pending:
        return "pending"
    return state


def audio_file(key: str) -> Path | None:
    """Файл готового трека. Отмечает, что его слушают, — не чаще раза в час,
    чтобы перемотка не переписывала метаданные на каждый запрос."""
    path = _audio_path(key)
    meta = read_meta(key)
    if meta is None or meta.get("status") != "ready" or not path.is_file():
        return None
    if time.time() - (meta.get("used") or 0) > 3600:
        _update_meta(key, used=time.time())
    return path


def cleanup(now: float | None = None) -> int:
    """Убрать отслужившее. Возвращает, сколько записей удалено."""
    now = time.time() if now is None else now
    folder = _dir()
    if not folder.is_dir():
        return 0
    removed = 0
    with _META_LOCK:
        for meta_path in folder.glob("*.json"):
            key = meta_path.stem
            meta = read_meta(key)
            if meta is None:
                meta_path.unlink(missing_ok=True)
                _audio_path(key).unlink(missing_ok=True)
                removed += 1
                continue
            state = meta.get("status")
            if state == "ready":
                seen = max(meta.get("used") or 0, meta.get("fetched") or 0, meta.get("at") or 0)
                age = now - seen
                limit = KEEP_SECONDS
            elif state == "failed":
                age, limit = now - (meta.get("failed_at") or meta.get("at") or 0), FAILED_SECONDS
            else:
                age, limit = now - (meta.get("at") or 0), WANTED_SECONDS
            if age > limit and key not in _pending:
                meta_path.unlink(missing_ok=True)
                _audio_path(key).unlink(missing_ok=True)
                removed += 1
        # Звук без описания и недокачанные остатки — мусор после сбоя. Только
        # файлы с именем-ключом: «<ключ>.dl.m4a» — это скачивание, которое идёт
        # прямо сейчас, и его стережёт проверка возраста ниже.
        for audio in folder.glob("*.m4a"):
            if _KEY.fullmatch(audio.stem) and not _meta_path(audio.stem).exists():
                audio.unlink(missing_ok=True)
                removed += 1
        for partial in folder.glob("*.dl.*"):
            if now - partial.stat().st_mtime > 3600:
                partial.unlink(missing_ok=True)
    return removed


_last_cleanup = 0.0


def cleanup_now_and_then() -> None:
    global _last_cleanup
    if time.time() - _last_cleanup < 3600:
        return
    _last_cleanup = time.time()
    try:
        removed = cleanup()
        if removed:
            logger.info("Кэш треков со стороны: убрано %d", removed)
    except OSError as exc:
        logger.warning("Кэш треков со стороны не почистился: %s", exc)


# ---------------------------------------------------------------------------
# Что подходит: похожие артисты и их популярные треки
# ---------------------------------------------------------------------------


def _similar_cache() -> Path:
    return runtime.PROJECT / "similar-cache"


def _library_key(artist: str, title: str) -> tuple[str, str]:
    """По чему трек считается «тем же». Без гостей в названии: в фонотеке
    «На чиле (feat. Егор Крид, …)», у Deezer «На чиле» — это одна песня."""
    bare = " ".join(_norm(core_title(title)).split())
    return " ".join(_norm(similar.primary(artist)).split()), bare


def library_keys(rows: list[dict]) -> set[tuple[str, str]]:
    return {_library_key(row.get("artist") or "", row.get("title") or "") for row in rows}


def _gather(names: list[str], ask, budget_end: float) -> dict[str, list]:
    """Спросить про каждое имя параллельно, но не дольше, чем осталось."""
    if not names:
        return {}
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deezer")
    futures = {pool.submit(ask, name): name for name in names}
    wait(futures, timeout=max(budget_end - time.monotonic(), 0))
    # Не ждём опоздавших. Начатые доделают своё и оставят ответ в кэше, ещё не
    # начатые отменяются: иначе очередь из сотни имён переживала бы остановку
    # службы и задерживала её.
    pool.shutdown(wait=False, cancel_futures=True)
    answers = {}
    for future, name in futures.items():
        # Отменённое — тоже «done», но спрашивать у него исключение нельзя.
        if future.done() and not future.cancelled() and future.exception() is None:
            answers[name] = future.result() or []
    return answers


def candidates(
    seed_artists: list[str],
    have: set[tuple[str, str]],
    want: int,
    budget: float = CANDIDATE_BUDGET,
) -> list[dict]:
    """Треки, которых нет в фонотеке, но которые подходят к этим артистам.

    Сами артисты очереди тоже в деле: недостающий хит того, кого слушаешь, —
    ровно то, что подмешивает Spotify. Каждый кандидат помнит, к кому он
    подобран (``seed``), — по этому его и ставят рядом с нужным треком.
    """
    budget_end = time.monotonic() + budget
    cache = _similar_cache()

    seeds: list[str] = []
    for artist in seed_artists:
        name = similar.primary(artist)
        if len(name) >= 2 and name.lower() not in {s.lower() for s in seeds}:
            seeds.append(name)
        if len(seeds) >= SEED_LIMIT:
            break
    if not seeds or want <= 0:
        return []

    neighbours = _gather(
        seeds, lambda name: similar.similar_artists(name, cache)[:NEIGHBOURS_PER_SEED], budget_end
    )

    askers: dict[str, list[str]] = {}
    for seed in seeds:
        for name in [seed, *neighbours.get(seed, [])]:
            askers.setdefault(name, [])
            if seed not in askers[name]:
                askers[name].append(seed)
    tops = _gather(list(askers), lambda name: similar.top_tracks(name, cache, limit=5), budget_end)

    per_seed: dict[str, list[dict]] = {seed: [] for seed in seeds}
    seen: set[str] = set()
    for name, rows in tops.items():
        for row in rows:
            artist, title = row.get("artist") or "", row.get("title") or ""
            if not artist or not title or _library_key(artist, title) in have:
                continue
            key = key_for(artist, title)
            # Не нашедшийся на YouTube неделю не предлагается: иначе в каждой
            # очереди были бы места, заранее обречённые на пропуск.
            if key in seen or status(key) == "failed":
                continue
            seen.add(key)
            item = {
                "key": key,
                "artist": artist,
                "title": title,
                "duration": row.get("duration"),
                "album": row.get("album") or "",
                "cover": row.get("cover") or "",
                "seed": askers[name][0],
            }
            per_seed[item["seed"]].append(item)

    # По кругу между артистами очереди: иначе все находки достались бы
    # первому из них.
    rng = random.Random()
    for items in per_seed.values():
        rng.shuffle(items)
    ordered: list[dict] = []
    while len(ordered) < want and any(per_seed.values()):
        for seed in seeds:
            if per_seed[seed]:
                ordered.append(per_seed[seed].pop())
                if len(ordered) >= want:
                    break
    return ordered


def entry(item: dict) -> dict:
    """Строка очереди для трека со стороны — в том же виде, что и своя."""
    return {
        "path": PREFIX + item["key"],
        "artist": item["artist"],
        "title": item["title"],
        "duration": item.get("duration"),
        "tempo": None,
        "external": True,
        "outside": True,
    }


def weave(local: list[dict], found: list[dict], count: int, seed: int | None = None) -> list[dict]:
    """Вплести ``count`` треков со стороны в готовую очередь своих.

    Первым всегда играет своё, два чужих подряд не встают. Место распределяется
    равномерно по всей очереди, а трек на него берётся тот, что подобран к
    артисту перед ним, — так новое звучит рядом с тем, на что похоже.
    """
    rng = random.Random(seed)
    pool = list(found)
    count = min(count, len(pool), len(local))
    out: list[dict] = []
    left = count
    for position, item in enumerate(local):
        out.append(item)
        slots = len(local) - position
        if left <= 0 or rng.random() >= left / slots:
            continue
        before = similar.primary(item.get("artist") or "").lower()
        other = [c for c in pool if similar.primary(c["artist"]).lower() != before]
        fits = [c for c in other if c["seed"].lower() == before] or other or pool
        chosen = fits[0]
        pool.remove(chosen)
        out.append(entry(chosen))
        left -= 1
    return out


# ---------------------------------------------------------------------------
# Выбор версии на YouTube
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^\w\s]", " ", text)


def _words(text: str) -> set[str]:
    return set(_norm(text).split())


# Deezer пишет русские названия то кириллицей, то латиницей («Vozrast»), а
# YouTube — как загрузил автор («Возраст»). Сравниваем латиницей.
_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
        "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)  # fmt: skip


def _latin_words(text: str) -> set[str]:
    return set(_norm(text).translate(_LATIN).split())


_CREDITS = re.compile(r"\s*[(\[](?:feat|ft|prod|with|при уч)\b[^)\]]*[)\]]", re.IGNORECASE)


def core_title(title: str) -> str:
    """Название без приписок: «Vozrast (feat. Artur Kreem) (Prod. Babyboi)» —
    это «Vozrast». В названиях роликов гостей и битмейкеров пишут как попало
    или не пишут вовсе, и сравнивать по ним — терять верные ролики."""
    bare = _CREDITS.sub("", title or "")
    bare = re.split(r"\s(?:feat|ft)\.?\s", bare, maxsplit=1, flags=re.IGNORECASE)[0]
    return bare.strip() or (title or "")


def choose_video(
    entries: list[dict], artist: str, title: str, duration: float | None
) -> dict | None:
    """Лучший ролик из выдачи поиска, или None, если подходящего нет.

    Без совпадения по длительности ролик не берётся вовсе: лучше пропустить
    трек, чем сыграть вместо него концерт.
    """
    wanted_title = _latin_words(core_title(title))
    topic = _norm(f"{similar.primary(artist)} - topic").strip()
    best, best_score = None, None
    for item in entries or []:
        length = item.get("duration")
        name = item.get("title") or ""
        channel = _norm(item.get("channel") or item.get("uploader") or "").strip()
        if not length or not item.get("id"):
            continue
        if duration:
            if abs(length - duration) > DURATION_TOLERANCE:
                continue
        elif length > 600:
            continue
        text = _norm(name)
        bad = {m.group(0) for m in _UNWANTED.finditer(text)}
        # Словами, а не подстрокой: «live» сидит внутри «Alive».
        if any(not set(word.split()) <= _words(title) for word in bad):
            continue
        is_topic = channel == topic
        if not is_topic and not wanted_title <= _latin_words(name):
            continue
        if not duration and not is_topic and "audio" not in text:
            # Без длительности верим только официальному звуку.
            continue
        score = (3 if is_topic else 0) + (1 if "audio" in text else 0)
        if duration:
            score -= abs(length - duration) / DURATION_TOLERANCE
        if best_score is None or score > best_score:
            best, best_score = item, score
    return best


# ---------------------------------------------------------------------------
# Скачивание в фоне
# ---------------------------------------------------------------------------

_jobs: queue.Queue = queue.Queue()
_pending: set[str] = set()
_pending_lock = threading.Lock()
_worker: threading.Thread | None = None


def request(keys: list[str]) -> dict[str, str]:
    """Попросить скачать. Возвращает состояние каждого ключа после просьбы."""
    global _worker
    answer = {}
    for key in keys:
        if not _KEY.fullmatch(key or ""):
            continue
        state = status(key)
        if state == "wanted":
            with _pending_lock:
                if key not in _pending:
                    _pending.add(key)
                    _jobs.put(key)
            state = "pending"
        answer[key] = state
    with _pending_lock:
        if _pending and (_worker is None or not _worker.is_alive()):
            _worker = threading.Thread(target=_work, name="outside-fetch", daemon=True)
            _worker.start()
    return answer


def _work() -> None:
    global _worker
    try:
        while not runtime.shutdown_event.is_set():
            try:
                key = _jobs.get(timeout=1)
            except queue.Empty:
                with _pending_lock:
                    if not _pending:
                        # Под замком и до выхода: request() увидит None и
                        # запустит новый поток, а не решит, что этот работает.
                        _worker = None
                        cleanup_now_and_then()
                        return
                continue
            try:
                fetch(key)
            except runtime.ShutdownRequested:
                return
            except Exception as exc:  # noqa: BLE001 — одна неудача не должна глушить остальные
                logger.info("Трек со стороны %s не скачался: %s", key, exc)
                try:
                    _update_meta(
                        key, status="failed", failed_at=time.time(), reason=str(exc)[-300:]
                    )
                except OSError as write_error:
                    logger.warning("Не записал неудачу %s: %s", key, write_error)
            finally:
                with _pending_lock:
                    _pending.discard(key)
    finally:
        # Как бы ни кончился цикл — даже исключением, — следующий request()
        # должен знать, что потока нет, и запустить новый.
        with _pending_lock:
            if _worker is threading.current_thread():
                _worker = None


def _search(query: str) -> list[dict]:
    done = ingest.run_yt_dlp(
        [*ingest.ytdlp_base(), "--flat-playlist", "-J", f"ytsearch{SEARCH_RESULTS}:{query}"],
        timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[-300:])
    return (json.loads(done.stdout) or {}).get("entries") or []


def _download(video_id: str, key: str) -> Path:
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob(f"{key}.dl.*"):
        stale.unlink(missing_ok=True)
    done = ingest.run_yt_dlp(
        [
            *ingest.ytdlp_base(),
            "-x",
            "--audio-format",
            "m4a",
            "--audio-quality",
            "0",
            "--no-playlist",
            "-o",
            str(folder / f"{key}.dl.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        timeout=300,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[-300:])
    produced = folder / f"{key}.dl.m4a"
    if not produced.is_file():
        found = sorted(folder.glob(f"{key}.dl.*"))
        if not found:
            raise RuntimeError("файл не найден после скачивания")
        produced = found[0]
    return produced


def _tag(path: Path, meta: dict) -> None:
    """Теги и обложка — чтобы плеер показал трек как свой, а кнопка «в
    фонотеку» отдала импорту уже подписанный файл."""
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(path)
    audio["\xa9nam"] = [meta["title"]]
    audio["\xa9ART"] = [meta["artist"]]
    if meta.get("album"):
        audio["\xa9alb"] = [meta["album"]]
    if meta.get("cover"):
        try:
            got = httpx.get(meta["cover"], timeout=10)
            if got.is_success and got.content:
                png = got.content[:4] == b"\x89PNG"
                kind = MP4Cover.FORMAT_PNG if png else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(got.content, imageformat=kind)]
        except httpx.HTTPError as exc:
            logger.info("Обложка для %s не скачалась: %s", meta["title"], exc)
    audio.save()


def fetch(key: str) -> None:
    meta = read_meta(key)
    if meta is None:
        return
    if meta.get("status") == "ready" and _audio_path(key).is_file():
        return
    has_space, free_mb = ingest.check_disk_space()
    if not has_space:
        raise RuntimeError(f"мало места на диске: {free_mb} МБ")

    entries = _search(f"{similar.primary(meta['artist'])} - {meta['title']}")
    video = choose_video(entries, meta["artist"], meta["title"], meta.get("duration"))
    if video is None:
        raise RuntimeError("на YouTube нет версии той же длины")

    produced = _download(video["id"], key)
    try:
        valid, problem = ingest.validate_audio_integrity(produced)
        if not valid:
            raise RuntimeError(problem)
        _tag(produced, meta)
        produced.replace(_audio_path(key))
    finally:
        produced.unlink(missing_ok=True)
    now = time.time()
    _update_meta(key, status="ready", fetched=now, used=now, video=video["id"])
    logger.info("Трек со стороны готов: %s — %s", meta["artist"], meta["title"])
