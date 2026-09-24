"""Deciding between two copies of the same song.

When a download looks like a track already in the library it is stored anyway
and its task carries a warning (see ingest.find_similar_library_track): it may
be the better recording, and which one to keep is the listener's call. This is
where that call is made, with both files side by side.

Keeping one copy moves the other to the trash the way every deletion does, and
first puts the kept copy in its place in every playlist, so a playlist never
ends up pointing at a file that is gone.
"""

from __future__ import annotations

import logging

import mutagen

from . import config, db, library, playlists

logger = logging.getLogger(__name__)

WARNING_PREFIX = "Похоже на уже имеющийся трек: "


def _similar_path(task: dict) -> str | None:
    # Older tasks have only the warning text; newer ones the column as well.
    if task.get("similar_to"):
        return task["similar_to"]
    warning = task.get("warning") or ""
    return warning[len(WARNING_PREFIX) :] if warning.startswith(WARNING_PREFIX) else None


def _clear(tid: int) -> None:
    db.task_update(tid, warning=None, similar_to=None)


def describe(path: str) -> dict | None:
    """What helps to choose: tags, length, bitrate, size, where it came from."""
    absolute = config.LIBRARY / path
    if not absolute.is_file():
        return None
    row = next((r for r in library.library_index() if r["path"] == path), None) or {}
    bitrate = codec = None
    try:
        info = mutagen.File(absolute).info
        bitrate = round(getattr(info, "bitrate", 0) / 1000) or None
        codec = getattr(info, "codec", None) or absolute.suffix.lstrip(".")
    except Exception:
        codec = absolute.suffix.lstrip(".")
    return {
        "path": path,
        "title": row.get("title") or absolute.stem,
        "artist": row.get("artist") or "",
        "album": row.get("album") or "",
        "duration": row.get("duration"),
        "added": row.get("added"),
        "source": row.get("source") or "",
        "bitrate": bitrate,
        "codec": codec,
        "size": absolute.stat().st_size,
    }


def pairs() -> list[dict]:
    """Every unresolved pair whose two files are both still in the library.

    A pair where one side has since been deleted is not a question any more;
    its warning is cleared on the way.
    """
    found = []
    tasks = db.db_query(
        "SELECT id, result_path, warning, similar_to FROM tasks "
        "WHERE status = 'done' AND warning IS NOT NULL AND warning != '' ORDER BY id DESC"
    )
    for task in tasks:
        new = describe(task["result_path"]) if task["result_path"] else None
        existing_path = _similar_path(task)
        existing = describe(existing_path) if existing_path else None
        if new is None or existing is None:
            _clear(task["id"])
            continue
        found.append({"task": task["id"], "new": new, "existing": existing})
    return found


def resolve(tid: int, keep: str) -> dict:
    """keep = "new", "existing" or "both". Returns what was done."""
    if keep not in ("new", "existing", "both"):
        raise ValueError("keep must be 'new', 'existing' or 'both'")
    pair = next((p for p in pairs() if p["task"] == tid), None)
    if pair is None:
        raise LookupError("No such duplicate pair")
    if keep == "both":
        _clear(tid)
        return {"kept": [pair["new"]["path"], pair["existing"]["path"]], "removed": None}

    kept = pair[keep]["path"]
    removed = pair["existing" if keep == "new" else "new"]["path"]
    moved = playlists.swap_everywhere(removed, kept)
    library.delete_track(removed)
    if keep == "existing":
        db.task_update(tid, result_path=kept)
    _clear(tid)
    logger.info(
        "Duplicate resolved: kept %s, removed %s, playlists updated %d",
        kept,
        removed,
        moved,
        extra={"task_id": tid},
    )
    return {"kept": [kept], "removed": removed, "playlists": moved}
