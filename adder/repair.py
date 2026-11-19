"""«Клипы» — очередь на починку (этап 3 плана 19.11.2026).

Трек попал в подборку «Клипы» — значит, с его звуком что-то не так: клип со
сценами, концерт, заглушённые слова, обрезанный конец. Фоновая задача ищет
другие версии той же песни на YouTube Music (студийную, официальный клип,
чужие загрузки той же длины ±5 с), качает до трёх, сравнивает каждую с файлом
(``censor.compare``) и сверяет тихие места с синхронным текстом. Итог — в
таблице ``repairs``; плеер показывает карточку «Починка»: что не так, лучший
кандидат, «послушать место» у обеих версий и кнопки «Заменить», «Вставить
ссылку», «Оставить как есть». После замены трек сам уходит из «Клипов».

Пределы YouTube (06.11.2026 при 3 с между скачиваниями YouTube закрыл загрузки
на час — и службе тоже): между скачиваниями не меньше PAUSE_SECONDS, не больше
DOWNLOADS_PER_HOUR в час, и только пока очередь добавления пуста — своё
человеку важнее.

Замена — на тот же путь и с тем же расширением .m4a: иначе пропадут звёзды
Navidrome и скачанное на айфоне. Теги и обложка — старого файла, старый звук —
в REPAIR_BACKUP.

Решения владельца (19.11.2026): когда не годятся ни клип, ни студийная, искать
другие источники — служба сама кладёт такой трек в «Клипы» (``queue_auto``);
студийная со словами про наркотики из любых путей добавления — на проверку
(``probe_later``): сначала текст, и только при таких словах — в «Клипы».
Поставленное службой и оказавшееся в порядке уходит из «Клипов» само.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, runtime

logger = logging.getLogger(__name__)

PLAYLIST = "Клипы"
DIR = runtime.PROJECT / "repair"
BACKUP_DIR = runtime.PROJECT / "repair-backup"

POLL_SECONDS = 60
PAUSE_SECONDS = 15.0  # как studio_audit.PAUSE_SECONDS
DOWNLOADS_PER_HOUR = 150
MAX_CANDIDATES = 3
UPLOAD_SLACK = 5.0  # чужая загрузка той же длины, что студийная, ±5 с
SCENE_SECONDS = 8.0  # столько лишнего звука до и после песни — вставки клипа
SHORTER_SECONDS = 10.0
EXCERPT_SECONDS = 6
MAX_ATTEMPTS = 3
RETRY_SECONDS = 3600

ACTIVE = ("queued", "working", "ready", "failed")
KIND_NAMES = {"studio": "студийная", "clip": "официальный клип", "upload": "чужая загрузка"}

_LOCK = threading.RLock()
_downloads: deque[float] = deque()
_last_download = 0.0
_thread: threading.Thread | None = None
# После «YouTube ограничил» фон молчит час целиком, а не до следующего трека
# (общая пауза службы — минута: человеку ждать час незачем).
_banned_until = 0.0
_wake = threading.Event()


class Retry(Exception):
    """Не вышло сейчас (YouTube Music или каталог текстов не ответили) — позже."""


# ---------------------------------------------------------------------------
# Строки таблицы
# ---------------------------------------------------------------------------


def _row(path: str) -> dict | None:
    rows = db.db_query("SELECT * FROM repairs WHERE path = ?", (path,))
    return dict(rows[0]) if rows else None


def _set(path: str, **fields) -> None:
    fields["updated_at"] = time.time()
    names = ", ".join(f"{k} = ?" for k in fields)
    db.db_exec(f"UPDATE repairs SET {names} WHERE path = ?", (*fields.values(), path))


def _insert(path: str, status: str, auto: bool = False, reason: str = "") -> None:
    db.db_exec(
        "INSERT OR REPLACE INTO repairs(path, status, auto, reason, result, error, attempts,"
        " next_at, updated_at) VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, ?)",
        (path, status, int(auto), reason, time.time()),
    )


def _key(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def _work_dir(path: str) -> Path:
    return DIR / _key(path)


def _drop_files(path: str) -> None:
    shutil.rmtree(_work_dir(path), ignore_errors=True)


def _stamp(file: Path) -> str:
    st = file.stat()
    # ctime: правка тегов на месте сохраняет mtime и часто размер.
    return f"{st.st_mtime_ns:x}-{st.st_size:x}-{st.st_ino:x}-{st.st_ctime_ns:x}"


def _library_row(path: str) -> dict | None:
    from . import library

    return next((r for r in library.library_index() if r.get("path") == path), None)


def _video_id(url: str) -> str:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    if parsed.hostname and parsed.hostname.endswith("youtu.be"):
        return parsed.path.strip("/")
    return (parse_qs(parsed.query).get("v") or [""])[0]


# ---------------------------------------------------------------------------
# Подборка «Клипы»
# ---------------------------------------------------------------------------


def playlist_paths() -> list[str] | None:
    """Треки «Клипов»; нет файла — пусто; None — не прочиталась (это не
    «пусто»: разборы из-за сбоя чтения не стираются)."""
    from . import playlists

    try:
        if not playlists.playlist_path(PLAYLIST).exists():
            return []
        return [entry.path for entry in playlists.read(PLAYLIST).entries]
    except Exception as exc:  # noqa: BLE001
        logger.info("Починка: «%s» не прочитана: %s", PLAYLIST, exc)
        return None


def _remove_from_playlist(*paths: str) -> None:
    from . import playlists

    gone = {p for p in paths if p}
    if not gone or not playlists.playlist_path(PLAYLIST).exists():
        return
    current = playlists.read(PLAYLIST)
    kept = [e.path for e in current.entries if e.path not in gone]
    if len(kept) != len(current.entries):
        playlists.write(PLAYLIST, kept, expected_revision=current.revision)


def _add_to_playlist(path: str) -> None:
    from . import playlists

    if playlists.playlist_path(PLAYLIST).exists():
        current = playlists.read(PLAYLIST)
        if any(e.path == path for e in current.entries):
            return
        playlists.write(PLAYLIST, [*(e.path for e in current.entries), path], current.revision)
    else:
        playlists.create(PLAYLIST, [path])


def refresh() -> None:
    """Сверить таблицу с подборкой: новое в «Клипах» — в очередь, убранное
    руками — забыть, заменённый мимо починки файл — разобрать заново."""
    with _LOCK:
        paths = playlist_paths()
        if paths is None:
            return
        present = set(paths)
        rows = {r["path"]: dict(r) for r in db.db_query("SELECT * FROM repairs")}
        for path in dict.fromkeys(paths):
            row = rows.get(path)
            if row is None or row["status"] in ("done", "kept", "probe"):
                # Положенное заново после «Оставить» — разобрать снова. «probe»
                # человек опередил: он сам положил трек в «Клипы».
                _insert(path, "queued", auto=False, reason=(row or {}).get("reason") or "")
                _wake.set()
            elif row["status"] == "ready":
                result = json.loads(row["result"] or "{}")
                try:
                    from . import library

                    stamp = _stamp(library.library_track(path))
                except Exception:  # noqa: BLE001 — файла нет: разбор покажет
                    stamp = ""
                if result.get("stamp") and stamp != result["stamp"]:
                    _drop_files(path)
                    _insert(path, "queued", bool(row["auto"]), row["reason"] or "")
                    _wake.set()
        for path, row in rows.items():
            if row["status"] in ACTIVE and path not in present:
                db.db_exec("DELETE FROM repairs WHERE path = ?", (path,))
                _drop_files(path)


# ---------------------------------------------------------------------------
# Пределы YouTube
# ---------------------------------------------------------------------------


def _users_turn() -> bool:
    """Очередь добавления занята — фоновая починка ждёт."""
    return (
        bool(runtime.PROCESSING_URLS)
        or runtime.youtube_pause_left() > 0
        or time.monotonic() < _banned_until
    )


def _wait_for_turn() -> None:
    """Дождаться права на скачивание: пауза после любого скачивания службы,
    место в часовом пределе, пустая очередь человека."""
    from . import ingest

    while not runtime.shutdown_event.is_set():
        now = time.monotonic()
        while _downloads and now - _downloads[0] > 3600:
            _downloads.popleft()
        last = max(_last_download, getattr(ingest, "LAST_DOWNLOAD", 0.0))
        wait = last + PAUSE_SECONDS - now
        if len(_downloads) >= DOWNLOADS_PER_HOUR:
            wait = max(wait, _downloads[0] + 3600 - now)
        if wait <= 0 and not _users_turn():
            return
        runtime.shutdown_event.wait(max(min(wait, 30.0), 1.0))
    raise runtime.ShutdownRequested()


def _download(found, into: Path) -> Path:
    global _last_download
    from . import ingest

    _wait_for_turn()
    _downloads.append(time.monotonic())
    _last_download = time.monotonic()
    key = f"repair_{found.video_id}_{int(time.time())}"
    try:
        temp = ingest.yt_download(found.url, key)
    finally:
        _last_download = time.monotonic()
    into.mkdir(parents=True, exist_ok=True)
    target = into / f"{found.video_id}.m4a"
    shutil.move(str(temp), str(target))
    return target


# ---------------------------------------------------------------------------
# Разбор одного трека
# ---------------------------------------------------------------------------


def choose(found: list, current_id: str, reference: float | None) -> list:
    """До MAX_CANDIDATES версий для скачивания: лучшая студийная (с «18+»
    первой), официальный клип, чужие загрузки длины студийной ±5 с."""
    found = [f for f in found if f.video_id and f.video_id != current_id]
    studios = sorted((f for f in found if f.kind == "studio"), key=lambda f: (not f.explicit,))
    clips = [f for f in found if f.kind == "clip"]
    uploads = [
        f
        for f in found
        if f.kind == "upload"
        and reference
        and f.duration
        and abs(f.duration - reference) <= UPLOAD_SLACK
    ]
    chosen = studios[:1] + clips[:1]
    for upload in uploads:
        if len(chosen) >= MAX_CANDIDATES:
            break
        chosen.append(upload)
    return chosen[:MAX_CANDIDATES]


def _clock(seconds: float) -> str:
    minute, second = divmod(int(max(seconds, 0)), 60)
    return f"{minute}:{second:02d}"


def assess(
    current_seconds: float,
    compared: list[dict],
    synced: list[dict],
    lyrics_seconds: float | None = None,
    song_seconds: float | None = None,
) -> dict:
    """Что не так с файлом и какая версия лучше — по сравнениям.

    ``compared`` — [{"video_id", "kind", "duration", "compared": censor.compare
    (файл, кандидат)}]. Время мест в сравнении — время файла; время кандидата —
    ``t - lag``. Синхронный текст размечен по студийной: слово на месте файла
    ищется по сдвигу студийной, а без неё — по сдвигу самого кандидата, если он
    той же длины, что текст (``synced`` взят по длине ``lyrics_seconds``).
    ``song_seconds`` — длина песни (студийная из выдачи) для «короче» и
    «сцен»; нет — длина первого кандидата. ``lyrics_seconds`` — только для
    правила «файл сам во времени текста».
    """
    from . import censor
    from .censor_words import drug_hits, hits

    studio = next(
        (
            c
            for c in compared
            if c["kind"] == "studio" and c["compared"].get("overall", 0) >= censor.SAME_MIX
        ),
        None,
    )
    reference = (
        (studio or {}).get("duration")
        or song_seconds
        # Клип за длину песни не годится: у него бывают вступление и сцены.
        or next(
            (c["duration"] for c in compared if c.get("duration") and c["kind"] != "clip"), None
        )
    )
    words_checked = False
    problems: list[dict] = []
    seen: set[str] = set()

    def problem(kind: str, text: str, at: float | None = None) -> None:
        if kind not in seen:
            seen.add(kind)
            problems.append({"kind": kind, "text": text, "at": at})

    rated = []
    for cand in compared:
        c = cand["compared"]
        same = c.get("overall", 0.0) >= censor.SAME_MIX
        lag = c.get("lag", 0.0)
        if studio is not None:
            shift = studio["compared"].get("lag", 0.0)
        elif lyrics_seconds and abs(current_seconds - lyrics_seconds) <= UPLOAD_SLACK:
            # Файл сам длиной с текст (студийная из YouTube Music, а её
            # видео в кандидаты не берётся): его время — время текста.
            shift = 0.0
        elif (
            same
            and reference
            and cand.get("duration")
            and abs(cand["duration"] - reference) <= UPLOAD_SLACK
        ):
            shift = lag
        else:
            shift = None
        cand_seconds = c.get("studio_seconds") or cand.get("duration") or 0.0
        file_seconds = c.get("file_seconds") or current_seconds
        muted_here: list[tuple[float, str]] = []
        muted_there: list[tuple[float, str]] = []
        unsure: list[tuple[float, str]] = []
        if same and shift is not None and synced:
            words_checked = True
            for start, end, kind in c.get("spots", []):
                if kind == "file":
                    word = censor.word_in_lines(synced, start - shift, end - shift, hits)
                    if word:
                        muted_here.append((start, word))
                elif kind == "studio":
                    word = censor.word_in_lines(synced, start - shift, end - shift, hits)
                    if word:
                        muted_there.append((start, word))
                elif kind == "other":
                    word = censor.word_in_lines(synced, start - shift, end - shift, drug_hits)
                    if word:
                        unsure.append((start, word))
        extra = (c.get("head") or 0.0) + max(c.get("tail") or 0.0, 0.0)
        longer = cand_seconds and file_seconds / cand_seconds > censor.LONGER_CLIP
        if same and extra > SCENE_SECONDS:
            problem(
                "scenes",
                f"Клип со вставками: лишние {round(extra)} с до и после песни",
                max((c.get("head") or 0.0) - 3, 0.0),
            )
        elif longer and cand["kind"] != "clip":
            problem(
                "scenes",
                f"Клип со вставками: {_clock(file_seconds)} против {_clock(cand_seconds)} у песни",
                0.0,
            )
        if muted_here:
            at, word = muted_here[0]
            problem("words", f"Заглушены слова: «{word}» ({_clock(at)})", at)
        if (
            same
            and reference
            and cand_seconds - file_seconds > SHORTER_SECONDS
            and abs(cand_seconds - reference) <= UPLOAD_SLACK
        ):
            problem(
                "shorter",
                f"Короче песни: {_clock(file_seconds)} против {_clock(cand_seconds)}",
                max(file_seconds - 8, 0.0),
            )
        has_scenes = reference is not None and cand_seconds - reference > SCENE_SECONDS
        rated.append(
            {
                **cand,
                "same": same,
                "lag": lag,
                "muted": [{"at": round(t, 1), "word": w} for t, w in muted_there],
                # Есть ли у версии слово там, где в файле тишина: сверено, а не
                # «ничего не нашлось» (копия того же подцензурного звука мест не даёт).
                "covers": [t for t, _ in muted_here],
                "unsure": [{"at": round(t, 1), "word": w} for t, w in unsure],
                "scenes": has_scenes,
            }
        )
    if compared and not any(r["same"] for r in rated) and "scenes" not in seen:
        if reference and current_seconds - reference > SCENE_SECONDS:
            # BLESSED (19.11.2026): клип 2:35 против песни 1:55, звук не
            # совпал ни с одной из трёх версий — сцены или другое сведение.
            problem(
                "other",
                f"Длиннее песни ({_clock(current_seconds)} против "
                f"{_clock(reference)}) и по звуку не совпадает ни с одной версией — "
                "клип со сценами или другая запись",
            )
        else:
            problem("other", "Не совпадает ни с одной версией — другая запись (концерт?)")

    words_wanted = "words" in seen
    # Все места, где в файле тишина на слове (по всем версиям): «лучше» —
    # только версия, у которой слышно каждое, а не одно из двух.
    quiet = [t for r in rated for t in r["covers"]]

    def covers_all(r: dict) -> bool:
        return bool(r["covers"]) and all(
            any(abs(t - mine) <= 2.0 for mine in r["covers"]) for t in quiet
        )

    def rank(r: dict) -> tuple:
        kind_rank = (
            {"upload": 0, "clip": 1, "studio": 2}
            if words_wanted
            else {"studio": 0, "upload": 1, "clip": 2}
        )[r["kind"]]
        off = abs((r.get("duration") or 0) - reference) if reference else 0.0
        # Сначала — сверенные с файлом: несовпавшую по звуку проверить было
        # нечем (Champagne Squirt 19.11.2026: «лучшей» стала загрузка со
        # сходством 0,06, а клип 0,98 — и есть оригинал). «unsure» — места,
        # где версия отличается от файла на словах про наркотики, — не минус:
        # у подцензурного файла так выглядит именно оригинал.
        # Заглушены слова — сначала версии, у которых слово на этом месте
        # слышно (сверено), а не те, где просто нет отличий: перезалив того же
        # подцензурного клипа отличий не даёт. Вид видео — только при равенстве.
        lacks = (words_wanted and not covers_all(r), words_wanted and not r["covers"])
        return (bool(r["muted"]), not r["same"], lacks, r["scenes"], kind_rank, off)

    ordered = sorted(rated, key=rank)
    best = ordered[0] if ordered else None
    better = (
        bool(problems)
        and best is not None
        and best["same"]
        and not best["muted"]
        and not best["scenes"]
        and (not words_wanted or covers_all(best))
    )
    place = None
    if best is not None:
        first = min((p["at"] for p in problems if p.get("at") is not None), default=None)
        if first is None:
            first = min(60.0, current_seconds / 3) if current_seconds else 0.0
        at = max(first - 1.0, 0.0)
        place = {"at": round(at, 1), "candidate_at": round(max(at - best["lag"], 0.0), 1)}
    return {
        "problems": problems,
        "candidates": [
            {
                k: r.get(k)
                for k in (
                    "video_id",
                    "kind",
                    "title",
                    "duration",
                    "explicit",
                    "url",
                    "muted",
                    "unsure",
                    "scenes",
                    "same",
                )
            }
            | {"overall": r["compared"].get("overall")}
            for r in ordered
        ],
        "best": best["video_id"] if best else None,
        "better": better,
        "place": place,
        "words_checked": words_checked,
    }


def _cut(source: Path, at: float, target: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(at, 0):.1f}", "-t", str(EXCERPT_SECONDS),
             "-i", str(source), "-vn", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4", str(target)],
            check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("Починка: кусок %s не вырезан: %s", source.name, exc)
        return False


def examine(path: str) -> dict:
    """Разобрать трек из «Клипов»: собрать версии, сравнить, выбрать лучшую."""
    from . import censor, library, similar, studio

    row = _library_row(path)
    if row is None:
        raise RuntimeError("Трека нет в фонотеке")
    file = library.library_track(path)
    stamp = _stamp(file)
    artist, title = row.get("artist") or "", row.get("title") or ""
    current_seconds = float(row.get("duration") or 0.0)
    found = studio.candidates(artist.replace(" • ", ", "), title)
    if found is None:
        raise Retry("YouTube Music не ответил")
    studios = [f for f in found if f.kind == "studio"]
    reference = next(
        (f.duration for f in sorted(studios, key=lambda f: not f.explicit) if f.duration), None
    )
    chosen = choose(found, _video_id(row.get("source") or ""), reference or current_seconds)
    work = _work_dir(path)
    compared: list[dict] = []
    failed: list[dict] = []
    if chosen and not censor.available():
        raise RuntimeError("Нет numpy/scipy — сравнивать нечем")
    for cand in chosen:
        info = {
            "video_id": cand.video_id,
            "kind": cand.kind,
            "title": cand.title,
            "duration": cand.duration,
            "explicit": cand.explicit,
            "url": cand.url,
        }
        try:
            downloaded = _download(cand, work)
        except runtime.ShutdownRequested:
            raise
        except Exception as exc:  # noqa: BLE001 — один кандидат — не весь разбор
            from . import ingest

            if ingest.classify_error(str(exc)) == "rate_limited":
                global _banned_until
                runtime.pause_youtube(config.RATE_LIMIT_BACKOFF)
                _banned_until = time.monotonic() + RETRY_SECONDS
                raise Retry("YouTube ограничил скачивания") from exc
            failed.append({**info, "error": str(exc)[-200:]})
            continue
        try:
            info["compared"] = censor.compare(file, downloaded)
        except Exception as exc:  # noqa: BLE001
            failed.append({**info, "error": f"сравнение не удалось: {exc}"[:200]})
            downloaded.unlink(missing_ok=True)
            continue
        compared.append(info)
    if chosen and not compared:
        # Сеть, yt-dlp, возрастной замок: «проблем нет» тут было бы неправдой.
        raise RuntimeError("ни одну версию не удалось скачать и сравнить: " + failed[0]["error"])
    synced: list[dict] = []
    lyrics_note = ""
    if compared:
        got = censor.synced_lyrics(similar.primary(artist), title, reference or current_seconds)
        if got is None:
            lyrics_note = "каталог текстов не ответил — слова не сверены"
        else:
            synced = got
            if not got:
                lyrics_note = "синхронного текста нет — слова не сверены"
    result = assess(
        current_seconds, compared, synced, reference or current_seconds, song_seconds=reference
    )
    result.update(
        {
            "stamp": stamp,
            "checked_at": time.time(),
            "failed": failed,
            "lyrics_note": lyrics_note,
            "searched": len(found),
            "compared": len(compared),
        }
    )
    place = result.get("place")
    best = result.get("best")
    if place and best:
        result["listen"] = {
            "current": _cut(file, place["at"], work / "listen-current.mp3"),
            "candidate": _cut(
                work / f"{best}.m4a", place["candidate_at"], work / "listen-candidate.mp3"
            ),
        }
    # Скачанное, кроме лучшего, больше не нужно: «Заменить» берёт лучший,
    # а другой кандидат — через «Вставить ссылку».
    for cand in compared:
        if cand["video_id"] != best:
            (work / f"{cand['video_id']}.m4a").unlink(missing_ok=True)
    return result


# ---------------------------------------------------------------------------
# Очередь
# ---------------------------------------------------------------------------


def _next() -> dict | None:
    rows = db.db_query(
        "SELECT * FROM repairs WHERE (status = 'queued'"
        " OR (status IN ('failed', 'probe') AND attempts < ?)) AND COALESCE(next_at, 0) <= ?"
        " ORDER BY updated_at LIMIT 1",
        (MAX_ATTEMPTS, time.time()),
    )
    return dict(rows[0]) if rows else None


def _probe(row: dict) -> None:
    """Студийная, добавленная мимо сверки: есть ли в тексте слова про
    наркотики. Есть — в «Клипы» на починку; нет — забыть."""
    from . import censor, similar

    path = row["path"]
    lib = _library_row(path)
    if lib is None:
        db.db_exec("DELETE FROM repairs WHERE path = ?", (path,))
        return
    synced = censor.synced_lyrics(
        similar.primary(lib.get("artist") or ""), lib.get("title") or "", lib.get("duration")
    )
    if synced is None:
        raise Retry("каталог текстов не ответил")
    lines = censor.drug_lines(synced)
    with _LOCK:
        if (_row(path) or {}).get("status") != "probe":
            return
        if not lines:
            db.db_exec("DELETE FROM repairs WHERE path = ?", (path,))
            return
        _set(path, status="queued", reason=row.get("reason") or "в тексте слова про наркотики")
        _add_to_playlist(path)


def process_one() -> bool:
    """Один шаг очереди. False — делать нечего."""
    row = _next()
    if row is None:
        return False
    path = row["path"]
    try:
        if row["status"] == "probe":
            _probe(row)
            return True
        with _LOCK:
            if (_row(path) or {}).get("status") not in ("queued", "failed"):
                return True
            _set(path, status="working", error=None)
        result = examine(path)
    except runtime.ShutdownRequested:
        with _LOCK:
            if (_row(path) or {}).get("status") == "working":
                _set(path, status="queued")
        raise
    except Retry as exc:
        with _LOCK:
            current = _row(path)
            if current:
                status = "probe" if current["status"] == "probe" else "queued"
                _set(path, status=status, error=str(exc), next_at=time.time() + RETRY_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 — один трек не должен глушить очередь
        logger.warning("Починка %s не удалась: %s", path, exc)
        with _LOCK:
            current = _row(path)
            if current and current["status"] == "probe":
                # Проверка текста — не починка: в «failed» её не показывать.
                _set(
                    path,
                    error=str(exc)[-300:],
                    attempts=(current["attempts"] or 0) + 1,
                    next_at=time.time() + RETRY_SECONDS,
                )
            elif current:
                _set(
                    path,
                    status="failed",
                    error=str(exc)[-300:],
                    attempts=(current["attempts"] or 0) + 1,
                    next_at=time.time() + RETRY_SECONDS,
                )
        return True
    with _LOCK:
        current = _row(path)
        if not current or current["status"] != "working":
            _drop_files(path)  # пока разбирали, трек убрали или оставили
            return True
        if (
            current["auto"]
            and result.get("compared")
            and result.get("words_checked")
            and not result["problems"]
        ):
            _set(path, status="kept", result=json.dumps(result, ensure_ascii=False), error=None)
            _drop_files(path)
            _remove_from_playlist(path)
            logger.info("Починка: %s проверен — отличий нет, убран из «%s»", path, PLAYLIST)
            return True
        _set(path, status="ready", result=json.dumps(result, ensure_ascii=False), error=None)
    logger.info(
        "Починка: %s разобран — %s",
        path,
        "; ".join(p["text"] for p in result["problems"]) or "отличий нет",
    )
    return True


def _loop() -> None:
    last_refresh = 0.0
    if runtime.shutdown_event.wait(30):
        return
    while not runtime.shutdown_event.is_set():
        try:
            if time.monotonic() - last_refresh >= POLL_SECONDS:
                refresh()
                last_refresh = time.monotonic()
            if _users_turn() or not process_one():
                _wake.wait(POLL_SECONDS)
                _wake.clear()
        except runtime.ShutdownRequested:
            return
        except Exception:  # noqa: BLE001 — поток не должен умирать
            logger.exception("Починка: проход упал")
            runtime.shutdown_event.wait(POLL_SECONDS)


def _reset_working() -> None:
    """Перезапуск посреди разбора (поток — daemon) оставил бы «working»
    навсегда: «Ищу и сравниваю…» без конца и без «Повторить»."""
    db.db_exec("UPDATE repairs SET status = 'queued' WHERE status = 'working'")


def start() -> threading.Thread | None:
    global _thread
    if runtime.under_test():
        return None
    _reset_working()
    _thread = threading.Thread(target=_loop, name="repair", daemon=True)
    _thread.start()
    return _thread


# ---------------------------------------------------------------------------
# Действия из плеера
# ---------------------------------------------------------------------------


class Refused(Exception):
    """Действие нельзя выполнить; текст — для человека."""


def listing() -> dict:
    """Карточки для страницы «Клипов» и счётчик для главной."""
    refresh()
    items = []
    for path in dict.fromkeys(playlist_paths() or []):
        row = _row(path)
        if row is None:
            continue
        result = json.loads(row["result"] or "null")
        item = {
            "path": path,
            "status": row["status"],
            "auto": bool(row["auto"]),
            "reason": row["reason"] or "",
            "error": row["error"] or "",
            "attempts": row["attempts"] or 0,
        }
        if result:
            item.update(
                {
                    "problems": result.get("problems", []),
                    "candidates": result.get("candidates", []),
                    "best": result.get("best"),
                    "better": result.get("better", False),
                    "place": result.get("place"),
                    "listen": result.get("listen") or {},
                    "failed": result.get("failed", []),
                    "lyrics_note": result.get("lyrics_note", ""),
                    "compared": result.get("compared", 0),
                }
            )
        items.append(item)
    return {
        "playlist": PLAYLIST,
        "items": items,
        "ready": sum(1 for i in items if i["status"] == "ready"),
        "working": sum(1 for i in items if i["status"] in ("queued", "working")),
    }


def counts() -> dict:
    """Для главной: сколько в «Клипах» разобрано и сколько ещё разбирается."""
    present = set(playlist_paths() or [])
    ready = working = 0
    for row in db.db_query("SELECT path, status FROM repairs"):
        if row["path"] not in present:
            continue
        if row["status"] == "ready":
            ready += 1
        elif row["status"] in ("queued", "working"):
            working += 1
    return {"ready": ready, "working": working, "playlist": PLAYLIST}


def excerpt(path: str, which: str) -> Path:
    if which not in ("current", "candidate"):
        raise Refused("Нет такого куска")
    file = _work_dir(path) / f"listen-{which}.mp3"
    if not file.is_file():
        raise Refused("Куска нет — разбор ещё не готов или уже закончен")
    return file


def _swap_in_place(rel: str, new: Path, url: str, expected_stamp: str) -> None:
    """Звук ``new`` — на место файла ``rel``: тот же путь и имя, теги и обложка
    старого, старый звук — в BACKUP_DIR (первый не перезаписывается)."""
    from mutagen.mp4 import MP4

    from . import ingest, library, loudness

    target = library.library_track(rel)
    runtime.guard_real_library(config.LIBRARY, "replace a track in place")
    if target.suffix.lower() != ".m4a":
        raise Refused("Заменить на месте можно только .m4a — вставь ссылку")
    source = MP4(target)
    fresh = MP4(new)
    if fresh.tags is None:
        fresh.add_tags()
    assert fresh.tags is not None
    fresh.tags.clear()
    for key, value in (source.tags or {}).items():
        low = key.lower()
        if low == "----:com.apple.itunes:itunsmpb":
            continue  # задержка кодера AAC — для Opus неверна
        if "replaygain" in low or "itunnorm" in low:
            continue  # громкость старого звука; loudness.apply меряет новый
        fresh.tags[key] = value
    fresh.save()
    ingest.write_source(new, url)
    loudness.apply(new)
    backup = BACKUP_DIR / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    with runtime.FILE_LOCK:
        before = target.stat()
        if _stamp(target) != expected_stamp:
            raise Refused("Трек изменился после разбора — разберу заново")
        if not backup.exists() or backup.stat().st_size == 0:
            # Через .part: оборванная копия не должна сойти за бэкап.
            part = backup.with_name(backup.name + ".part")
            shutil.copy2(target, part)
            os.replace(part, backup)
        os.chmod(new, before.st_mode & 0o7777)
        staged = target.with_name(f".{target.name}.repairing")
        try:
            shutil.move(str(new), staged)
            # Дата добавления в фонотеку — mtime: её не трогаем.
            os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    _after_swap(rel)


def _after_swap(rel: str) -> None:
    """Всё, что держалось за старый звук того же пути."""
    from . import library, lyrics, navidrome, playlists

    for step, call in (
        ("разбор звука", lambda: db.db_exec("DELETE FROM audio_features WHERE path = ?", (rel,))),
        ("длина в подборках", lambda: playlists.refresh_lengths(rel)),
        ("текст", lambda: lyrics.forget(rel)),
        (
            "отметка для телефона",
            lambda: db.db_exec(
                "INSERT OR REPLACE INTO replaced_in_place(path, at) VALUES (?, ?)",
                (rel, time.time()),
            ),
        ),
        ("Navidrome", navidrome.request_full_scan),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 — звук уже на месте
            logger.warning("Починка %s: %s не обновлено: %s", rel, step, exc)
    library.invalidate_library_index()


def replace(path: str, video_id: str) -> dict:
    """«Заменить»: лучший (или названный) кандидат — на место файла."""
    with _LOCK:
        row = _row(path)
        if row is None or row["status"] != "ready":
            raise Refused("Разбор этого трека не готов")
        result = json.loads(row["result"] or "{}")
        cand = next((c for c in result.get("candidates", []) if c["video_id"] == video_id), None)
        if cand is None:
            raise Refused("Такого кандидата нет")
        file = _work_dir(path) / f"{video_id}.m4a"
        if not file.is_file():
            raise Refused("Скачанной версии уже нет — вставь ссылку или разбери заново")
        staged = _work_dir(path) / f"{video_id}.staged.m4a"
        shutil.copy2(file, staged)
        try:
            _swap_in_place(path, staged, cand["url"], result.get("stamp") or "")
        except Refused as exc:
            staged.unlink(missing_ok=True)
            if "изменился" in str(exc):
                _set(path, status="queued", result=None)
                _drop_files(path)
                _wake.set()
            raise
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        note = f"Заменён: {KIND_NAMES.get(cand['kind'], cand['kind'])} «{cand['title']}»"
        result["outcome"] = note
        _set(path, status="done", result=json.dumps(result, ensure_ascii=False))
        _drop_files(path)
        try:
            _remove_from_playlist(path)
        except Exception as exc:  # noqa: BLE001 — файл уже заменён: это успех
            logger.warning("Починка: %s заменён, но не убран из «%s»: %s", path, PLAYLIST, exc)
            note += " (из «Клипов» убери сам)"
    logger.info("Починка: %s — %s", path, note)
    return {"path": path, "replaced": True, "note": note}


def keep(path: str) -> dict:
    """«Оставить как есть»: убрать из «Клипов», не трогая файл."""
    with _LOCK:
        row = _row(path)
        if row is None:
            _insert(path, "kept")
        else:
            _set(path, status="kept")
        _drop_files(path)
        try:
            _remove_from_playlist(path)
        except Exception as exc:  # noqa: BLE001 — «оставлен» уже записано
            logger.warning("Починка: %s не убран из «%s»: %s", path, PLAYLIST, exc)
    return {"path": path, "kept": True}


def retry(path: str) -> dict:
    with _LOCK:
        if _row(path) is None:
            raise Refused("Этого трека нет в «Клипах»")
        _drop_files(path)
        _set(path, status="queued", attempts=0, next_at=0, error=None, result=None)
    _wake.set()
    return {"path": path, "queued": True}


def after_replacement(old_path: str, new_path: str) -> None:
    """Трек заменён «Заменить по ссылке» (в том числе из карточки): из
    «Клипов» он уходит сам. Ошибка здесь замену не отменяет."""
    try:
        with _LOCK:
            in_list = set(playlist_paths() or [])
            if old_path not in in_list and new_path not in in_list:
                return
            for path in {old_path, new_path}:
                row = _row(path)
                if row is not None:
                    _set(path, status="done")
                    _drop_files(path)
            _remove_from_playlist(old_path, new_path)
        logger.info("Починка: %s заменён по ссылке — убран из «%s»", old_path, PLAYLIST)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Починка: %s не убран из «%s»: %s", old_path, PLAYLIST, exc)


def queue_auto(path: str, reason: str) -> None:
    """Служба сама кладёт трек в «Клипы»: не годятся ни клип, ни студийная
    (ответ владельца 19.11.2026 — «искать другие источники»)."""
    try:
        with _LOCK:
            row = _row(path)
            if row is not None and row["status"] in ACTIVE:
                return
            _insert(path, "queued", auto=True, reason=reason)
            _add_to_playlist(path)
        _wake.set()
    except Exception as exc:  # noqa: BLE001 — добавление трека уже удалось
        logger.warning("Починка: %s не поставлен в «%s»: %s", path, PLAYLIST, exc)


def probe_later(path: str, reason: str) -> None:
    """Студийная добавлена без сверки с клипом: посмотреть текст позже."""
    try:
        with _LOCK:
            row = _row(path)
            # «done»/«kept» — прошлый разбор другого файла на этом пути: новый
            # (например, студийная по «Вставить ссылку») проверить заново.
            if row is not None and row["status"] not in ("done", "kept"):
                return
            _insert(path, "probe", auto=True, reason=reason)
        _wake.set()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Починка: %s не поставлен на проверку: %s", path, exc)


def replaced_since(since: float) -> list[dict]:
    """Пути, заменённые на месте после ``since`` (не старше 30 дней)."""
    floor = max(since, time.time() - 30 * 24 * 3600)
    rows = db.db_query("SELECT path, at FROM replaced_in_place WHERE at > ? ORDER BY at", (floor,))
    return [{"path": r["path"], "at": r["at"]} for r in rows]
