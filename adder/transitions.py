"""Переход «как диджей»: связки между треками, их очередь и кэш.

Плеер на ноутбуке, пока играет трек A, просит связку A → B для следующего
трека. Связку сводит scripts/render_transition.py (librosa, rubberband):
секунд пять на пару, сотни мегабайт памяти. Поэтому — как разбор фонотеки —
отдельным процессом, с потолком памяти и пониженным приоритетом, по одному
за раз, и только по просьбе плеера.

Готовая связка лежит в TRANSITIONS_DIR: <ключ>.flac и <ключ>.json (план).
Ключ — от версии сведения, обоих файлов (путь, размер, время изменения) и
того, что влияет на звук (громкость, темп, голос). Изменился файл — ключ
другой, старая связка просто вытесняется: хранятся последние KEEP.

Не сводятся: трек не из фонотеки (со стороны), короткие треки и соседи по
альбому (номер +1 на том же альбоме): там переход задуман самим альбомом.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import HTTPException

from . import analysis, config, db, library, lyrics, runtime

logger = logging.getLogger(__name__)

RENDER_VERSION = 1  # то же, что VERSION в scripts/render_transition.py
SCRIPT = runtime.PROJECT.parent / "scripts" / "render_transition.py"
KEEP = 60  # связок на диске (обычно 2–3 МБ, длинное наложение — до ~9)
MIN_A = 60.0  # с: короче — переход съел бы заметную часть трека
MIN_B = 30.0
FAILED_TTL = 3600.0  # не сводить ту же пару заново раньше, чем через час
QUEUE_MAX = 4  # плеер просит одну-две пары; больше — устаревшие просьбы
TIMEOUT = 180

_KEY = re.compile(r"[0-9a-f]{64}")  # только fullmatch: `$` пропустил бы \n в конце


class RenderError(Exception):
    pass


_lock = threading.Lock()
_pending: dict[str, dict] = {}  # ключ → задание, ждущее или идущее
_failed: dict[str, float] = {}  # ключ → когда не вышло
_jobs: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None


def reset() -> None:
    """Для тестов: забыть очередь и неудачи (поток доработает сам и выйдет)."""
    global _jobs, _worker
    with _lock:
        _pending.clear()
        _failed.clear()
        _jobs = queue.Queue()
        _worker = None


def _row(path: str) -> dict | None:
    return next((r for r in library.library_index() if r["path"] == path), None)


def _relative(path: str) -> str:
    absolute = library.library_track(path)
    return str(absolute.relative_to(config.LIBRARY.resolve()))


def _album_neighbours(a: dict, b: dict) -> bool:
    """Номер +1 на том же альбоме того же исполнителя; сингл (альбом = название) — не альбом."""
    album = (a.get("album") or "").strip()
    if not album or album.casefold() == (a.get("title") or "").strip().casefold():
        return False
    if (b.get("album") or "").strip() != album:
        return False
    if (a.get("albumartist") or a.get("artist")) != (b.get("albumartist") or b.get("artist")):
        return False
    na, nb = a.get("track"), b.get("track")
    return isinstance(na, int) and isinstance(nb, int) and nb == na + 1


def _tempo(path: str) -> float | None:
    rows = db.db_query("SELECT tempo FROM audio_features WHERE path = ?", (path,))
    tempo = rows[0]["tempo"] if rows else None
    return float(tempo) if isinstance(tempo, (int, float)) and tempo > 0 else None


def _job(a: str, b: str, row_a: dict, row_b: dict) -> tuple[str, dict]:
    file_a, file_b = config.LIBRARY / a, config.LIBRARY / b
    sa, sb = file_a.stat(), file_b.stat()
    job = {
        "a": str(file_a),
        "b": str(file_b),
        "gain_a": row_a.get("gain"),
        "gain_b": row_b.get("gain"),
        "tempo_a": _tempo(a),
        "tempo_b": _tempo(b),
        "voice_b": lyrics.first_voice(b),
    }
    identity = [RENDER_VERSION, a, sa.st_size, sa.st_mtime_ns, b, sb.st_size, sb.st_mtime_ns]
    identity += [job["gain_a"], job["gain_b"], job["tempo_a"], job["tempo_b"], job["voice_b"]]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return key, job


def _paths(key: str) -> tuple[Path, Path]:
    folder = runtime.TRANSITIONS_DIR
    return folder / f"{key}.flac", folder / f"{key}.json"


def request(a: str, b: str) -> dict:
    """Связка A → B: готова (план), собирается или не будет (почему)."""
    if a.startswith("outside:") or b.startswith("outside:"):
        return {"status": "none", "reason": "outside"}
    a, b = _relative(a), _relative(b)
    row_a, row_b = _row(a), _row(b)
    if row_a is None or row_b is None:
        raise HTTPException(status_code=404, detail="Track not found")
    if (row_a.get("duration") or 0) < MIN_A or (row_b.get("duration") or 0) < MIN_B:
        return {"status": "none", "reason": "short"}
    if _album_neighbours(row_a, row_b):
        return {"status": "none", "reason": "album"}
    key, job = _job(a, b, row_a, row_b)
    audio, plan_path = _paths(key)
    if audio.exists() and plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            return {"status": "ready", "key": key, "plan": plan}
        except (OSError, ValueError):
            pass  # битый план — собрать заново
    with _lock:
        now = time.time()
        for old in [k for k, at in _failed.items() if now - at >= FAILED_TTL]:
            del _failed[old]
        if key in _failed:
            return {"status": "none", "reason": "failed"}
        if key not in _pending:
            if not _available():
                return {"status": "none", "reason": "unavailable"}
            _pending[key] = job
            _jobs.put(key)
            _trim_queue()
        # Каждый раз, не только с новой просьбой: поток мог уйти (30 с тишины)
        # ровно после того, как она легла в очередь, — и её никто бы не взял.
        _ensure_worker()
    return {"status": "pending", "key": key}


def _trim_queue() -> None:
    """Оставить последние QUEUE_MAX просьб: плеер давно ушёл от старых."""
    if _jobs.qsize() <= QUEUE_MAX:
        return
    keys = []
    while True:
        try:
            keys.append(_jobs.get_nowait())
        except queue.Empty:
            break
    for key in keys[:-QUEUE_MAX]:
        _pending.pop(key, None)
    for key in keys[-QUEUE_MAX:]:
        _jobs.put(key)


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_work, args=(_jobs,), name="transitions", daemon=True)
        _worker.start()


def _work(jobs: queue.Queue) -> None:
    while not runtime.shutdown_event.is_set():
        try:
            key = jobs.get(timeout=30)
        except queue.Empty:
            return  # тихо — поток уходит, следующая просьба поднимет новый
        with _lock:
            job = _pending.get(key)
        if job is None:
            continue
        try:
            _build(key, job)
        except Exception as exc:  # noqa: BLE001 — неудача пары не валит службу
            logger.warning("Transition not made: %s", exc, extra={"task_id": "system"})
            with _lock:
                _failed[key] = time.time()
        finally:
            with _lock:
                _pending.pop(key, None)


def _build(key: str, job: dict) -> None:
    audio, plan_path = _paths(key)
    audio.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    plan = _render({**job, "out": str(audio)})
    if plan.get("version") != RENDER_VERSION:
        audio.unlink(missing_ok=True)
        raise RenderError(f"план версии {plan.get('version')}, ждали {RENDER_VERSION}")
    tmp = plan_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    tmp.replace(plan_path)  # план последним: есть план — есть и звук
    logger.info(
        "Transition %s → %s: %s in %.1f s",
        Path(job["a"]).name,
        Path(job["b"]).name,
        plan.get("case"),
        time.monotonic() - started,
        extra={"task_id": "system"},
    )
    _prune()


def _available() -> bool:
    """Есть ли чем сводить: скрипт, пакеты разбора у этого же Python и ffmpeg.

    Пакеты разбора ставятся отдельно (scripts/requirements-analysis.txt); без
    них плеер просто остаётся на обычном затухании."""
    if not SCRIPT.exists() or not shutil.which("ffmpeg"):
        return False
    return all(importlib.util.find_spec(name) for name in ("librosa", "scipy", "soundfile"))


def _render(job: dict) -> dict:
    """Запустить сведение отдельным процессом; план — из его вывода."""
    result = subprocess.run(
        # Тот же Python, что у службы: пакеты разбора проверены у него (_available).
        analysis.bounded([sys.executable, str(SCRIPT)]),
        input=json.dumps(job, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        answer = {"error": (result.stderr.strip() or "нет ответа")[-300:]}
    if result.returncode != 0 or "error" in answer:
        Path(job["out"]).unlink(missing_ok=True)
        raise RenderError(str(answer.get("error", f"код {result.returncode}")))
    return answer


def _prune() -> None:
    plans = sorted(runtime.TRANSITIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in plans[:-KEEP] if len(plans) > KEEP else []:
        old.with_suffix(".flac").unlink(missing_ok=True)
        old.unlink(missing_ok=True)
    # Звук без плана — недописанная связка (служба упала посередине).
    with _lock:
        busy = set(_pending)
    for audio in runtime.TRANSITIONS_DIR.glob("*.flac"):
        if not audio.with_suffix(".json").exists() and audio.stem not in busy:
            audio.unlink(missing_ok=True)


def audio_file(key: str) -> Path:
    if not _KEY.fullmatch(key):
        raise HTTPException(status_code=404, detail="No such transition")
    audio, plan_path = _paths(key)
    if not audio.is_file() or not plan_path.is_file():
        raise HTTPException(status_code=404, detail="No such transition")
    return audio
