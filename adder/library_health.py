"""What is wrong with the library, and one button for each thing that can be fixed.

scripts/audit_library.py answered this on the command line. The panel is where
the library is actually looked after, so the same questions are asked here from
the library index (already in memory, so the report is cheap) and the two
problems that have an automatic fix - missing covers and missing ReplayGain -
get a button that runs it in the background.
"""

from __future__ import annotations

import logging
import threading
import time

from . import config, fix_covers, library, loudness, runtime

logger = logging.getLogger(__name__)

# How many example paths each problem lists; the counts are always complete.
EXAMPLES = 20

FIXES = {
    "covers": "Поиск обложек",
    "loudness": "Измерение громкости",
}

_job: dict = {"what": None, "running": False, "started": None, "finished": None, "result": None}
_job_lock = threading.Lock()


def report() -> dict:
    rows = library.library_index()
    problems: dict[str, list[str]] = {
        "no_cover": [],
        "no_album": [],
        "fallback_single": [],
        "no_loudness": [],
    }
    for row in rows:
        path = row["path"]
        if not row.get("has_cover"):
            problems["no_cover"].append(path)
        if not row.get("album"):
            problems["no_album"].append(path)
        elif row.get("album") == row.get("title") and not row.get("track"):
            # Deezer did not know the track, so it was filed as its own single.
            problems["fallback_single"].append(path)
        if row.get("gain") is None:
            problems["no_loudness"].append(path)
    problems["unreadable"] = list(library.UNREADABLE)

    with _job_lock:
        job = dict(_job)
    return {
        "tracks": len(rows),
        "problems": {
            name: {"count": len(paths), "examples": paths[:EXAMPLES]}
            for name, paths in problems.items()
        },
        "job": job,
    }


def _run(what: str) -> None:
    started = time.time()
    result: dict[str, int | str]
    try:
        if what == "covers":
            added, missed = fix_covers.backfill(
                config.LIBRARY, config.DELAY_BETWEEN_TRACKS, sleep=runtime.shutdown_event.wait
            )
            result = {"added": added, "not_found": missed}
        else:
            measured, _, failed = loudness.backfill(config.LIBRARY, runtime.DB_PATH)
            result = {"measured": measured, "failed": failed}
    except Exception as exc:
        logger.exception("Library fix %s failed", what, extra={"task_id": "system"})
        result = {"error": str(exc)[:200]}
    library.invalidate_library_index()
    with _job_lock:
        _job.update(running=False, finished=time.time(), result=result)
    logger.info(
        "Library fix %s done in %.0fs: %s",
        what,
        time.time() - started,
        result,
        extra={"task_id": "system"},
    )


def start_fix(what: str) -> dict:
    """Start one fix in the background. Only one runs at a time."""
    if what not in FIXES:
        raise ValueError(f"Unknown fix: {what}")
    with _job_lock:
        if _job["running"]:
            raise RuntimeError(f"{FIXES[_job['what']]} ещё идёт")
        _job.update(what=what, running=True, started=time.time(), finished=None, result=None)
    threading.Thread(target=_run, args=(what,), name=f"library-fix-{what}", daemon=True).start()
    return dict(_job)
