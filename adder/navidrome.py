"""The Navidrome side of a playlist: its cover, and its removal.

Two things cannot be done by writing a file, and this module exists for them.

A playlist cover is stored in Navidrome's own database. Nothing in an .m3u can
carry it and no Subsonic client reads a picture from anywhere else.

A playlist whose .m3u disappears is *not* removed by Navidrome. Verified:
renaming the file produces a second playlist and leaves the first one standing;
deleting the file removes neither. So a rename is three operations -- move the
file, delete the old playlist here, re-upload the cover -- and a delete is two.

Neither is atomic with the file operation, and Navidrome may be down entirely,
so anything that fails is queued and retried rather than lost. Losing it
silently is how a playlist list fills up with corpses.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import config, db

logger = logging.getLogger(__name__)

_TOKEN: dict[str, Any] = {"value": None, "at": 0.0}
_TOKEN_TTL = 1800
_TOKEN_LOCK = threading.Lock()

TIMEOUT = 10


class NavidromeUnavailable(RuntimeError):
    """Navidrome could not be reached or refused the credentials."""


def configured() -> bool:
    return bool(config.NAVIDROME_USER and config.NAVIDROME_PASSWORD)


def _token() -> str:
    if not configured():
        raise NavidromeUnavailable("Navidrome credentials are not configured")
    with _TOKEN_LOCK:
        if _TOKEN["value"] and time.time() - float(_TOKEN["at"]) < _TOKEN_TTL:
            return str(_TOKEN["value"])
        try:
            response = requests.post(
                f"{config.NAVIDROME_URL}/auth/login",
                json={"username": config.NAVIDROME_USER, "password": config.NAVIDROME_PASSWORD},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            token = response.json()["token"]
        except Exception as exc:
            raise NavidromeUnavailable(f"Navidrome login failed: {exc}") from exc
        _TOKEN.update({"value": token, "at": time.time()})
        return token


def _headers() -> dict:
    return {"x-nd-authorization": f"Bearer {_token()}"}


def full_scan() -> None:
    """Ask Navidrome to read every file again, not only those with a new mtime.

    Its quick scan goes by modification time, and tools that rewrite audio in
    place keep that time on purpose (it is the track's "added" date).
    """
    import hashlib
    import secrets

    if not configured():
        raise NavidromeUnavailable("Navidrome credentials are not configured")
    salt = secrets.token_hex(8)
    digest = hashlib.md5((config.NAVIDROME_PASSWORD + salt).encode()).hexdigest()
    try:
        response = requests.get(
            f"{config.NAVIDROME_URL}/rest/startScan",
            params={
                "u": config.NAVIDROME_USER, "t": digest, "s": salt, "v": "1.16.1",
                "c": "local-Spotify", "f": "json", "fullScan": "true",
            },
            timeout=TIMEOUT,
        )  # fmt: skip
        response.raise_for_status()
        status = response.json()["subsonic-response"]["status"]
    except Exception as exc:
        # Never str(exc): a requests error carries the whole URL, and the
        # u/t/s triple in it logs in to Navidrome as that user.
        code = getattr(getattr(exc, "response", None), "status_code", None)
        detail = type(exc).__name__ + (f", HTTP {code}" if code else "")
        raise NavidromeUnavailable(f"Navidrome scan request failed ({detail})") from None
    if status != "ok":
        raise NavidromeUnavailable("Navidrome refused the scan (check NAVIDROME_USER/PASSWORD)")


def playlists() -> list[dict]:
    try:
        response = requests.get(
            f"{config.NAVIDROME_URL}/api/playlist", headers=_headers(), timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json() or []
    except NavidromeUnavailable:
        raise
    except Exception as exc:
        raise NavidromeUnavailable(f"Could not list Navidrome playlists: {exc}") from exc


def _same_file(entry: dict, name: str) -> bool:
    """Is this Navidrome playlist the one backed by our file for ``name``?

    By the file, not by the name: a playlist made on the phone can carry the
    same name, and deleting "Gym" must never take the phone's "Gym" with it —
    that one has no file and cannot be brought back.
    """
    from . import playlists as playlist_files

    if not entry.get("sync") or not entry.get("path"):
        return False
    try:
        ours = playlist_files.playlist_path(name)
    except Exception:
        return False
    return os.path.normpath(str(entry["path"])) == os.path.normpath(str(ours))


def find_id(name: str) -> str | None:
    for entry in playlists():
        if _same_file(entry, name):
            return entry.get("id")
    return None


def delete_playlist(name: str) -> bool:
    """Remove a playlist from Navidrome by name. True if something was removed."""
    playlist_id = find_id(name)
    if playlist_id is None:
        return False
    response = requests.delete(
        f"{config.NAVIDROME_URL}/api/playlist/{playlist_id}",
        headers=_headers(),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    logger.info("Navidrome playlist %r (%s) deleted", name, playlist_id)
    return True


class NotImportedYet(Exception):
    """Navidrome has not read the playlist file yet (it takes ~6 s after a
    write). Worth retrying — unlike "no such playlist", this passes."""


def upload_cover(name: str, image: bytes, filename: str) -> bool:
    """Attach an image to a playlist. True if Navidrome accepted it."""
    playlist_id = find_id(name)
    if playlist_id is None:
        # Right after create or rename the file is there but Navidrome has not
        # imported it; a False here used to be reported as "done" and the
        # cover never reached it.
        raise NotImportedYet(f"playlist {name!r} is not in Navidrome yet")
    response = requests.post(
        f"{config.NAVIDROME_URL}/api/playlist/{playlist_id}/image",
        headers=_headers(),
        files={"image": (filename, image)},
        timeout=TIMEOUT * 3,
    )
    response.raise_for_status()
    logger.info("Navidrome cover uploaded for %r (%s)", name, playlist_id)
    return True


# ---------------------------------------------------------------------------
# Deferred operations
# ---------------------------------------------------------------------------


def enqueue(op: str, name: str, payload: dict | None = None) -> None:
    """Record work that has to reach Navidrome eventually."""
    db.db_exec(
        "INSERT INTO navidrome_ops(op, name, payload, created_at) "
        "VALUES(?, ?, ?, datetime('now','localtime'))",
        (op, name, json.dumps(payload or {})),
    )
    logger.info("Queued Navidrome op %s for %r", op, name)


def _run(op: str, name: str, payload: dict) -> bool:
    if op == "delete":
        delete_playlist(name)
        return True
    if op == "cover":
        from . import covers

        image = covers.read_cover(name)
        if image is None:
            return True  # the local cover is gone; nothing left to replicate
        data, filename = image
        return upload_cover(name, data, filename)
    logger.warning("Unknown Navidrome op %r, dropping it", op)
    return True


def drain() -> dict:
    """Retry everything queued. Safe to call when Navidrome is down."""
    pending = db.db_query("SELECT id, op, name, payload FROM navidrome_ops ORDER BY id LIMIT 200")
    if not pending:
        return {"pending": 0, "done": 0}
    done = 0
    for row in pending:
        try:
            _run(row["op"], row["name"], json.loads(row["payload"] or "{}"))
        except Exception as exc:
            db.db_exec(
                "UPDATE navidrome_ops SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc)[:300], row["id"]),
            )
            logger.info("Navidrome op %s for %r still pending: %s", row["op"], row["name"], exc)
            continue
        db.db_exec("DELETE FROM navidrome_ops WHERE id = ?", (row["id"],))
        done += 1
    remaining = db.db_query("SELECT COUNT(*) AS n FROM navidrome_ops")[0]["n"]
    return {"pending": remaining, "done": done}


def pending_count() -> int:
    try:
        return db.db_query("SELECT COUNT(*) AS n FROM navidrome_ops")[0]["n"]
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

_RECONCILE_MARKER = "navidrome_reconcile_armed"


def reconcile(apply: bool | None = None) -> dict:
    """Delete Navidrome playlists whose backing .m3u is gone.

    Deliberately narrow. It only considers playlists Navidrome itself marks as
    file-backed -- ``sync`` true with a non-empty ``path``. Playlists made in
    Amperfy or in Navidrome's own web UI have no file by construction, and a
    naive "no file, delete it" sweep would erase them for good.

    The first run reports what it would remove and removes nothing. The marker
    that records "this has run once" lives in the database rather than in a
    file, so it survives moving the repository.
    """

    if apply is None:
        armed = db.db_query("SELECT value FROM settings WHERE key = ?", (_RECONCILE_MARKER,))
        apply = bool(armed)

    orphans = []
    for entry in playlists():
        if not entry.get("sync") or not entry.get("path"):
            continue
        name = entry.get("name") or ""
        # The file Navidrome says it read — not one rebuilt from the name, which
        # would call every playlist in a subfolder an orphan on each start.
        if not Path(str(entry["path"])).exists() and _same_file(entry, name):
            orphans.append(name)

    if apply:
        for name in orphans:
            try:
                delete_playlist(name)
            except Exception as exc:
                logger.warning("Could not delete orphaned playlist %r: %s", name, exc)
                enqueue("delete", name)
    else:
        if orphans:
            logger.info(
                "Reconciliation would remove %d orphaned Navidrome playlist(s): %s "
                "(reporting only on the first run)",
                len(orphans),
                ", ".join(orphans),
            )
        db.db_exec(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?, '1')",
            (_RECONCILE_MARKER,),
        )

    return {"orphans": orphans, "applied": bool(apply)}


# ---------------------------------------------------------------------------
# Reading a playlist back out of Navidrome
# ---------------------------------------------------------------------------

PAGE = 500


def remote_tracks(playlist_id: str, expected: int) -> list[str]:
    """The playlist's tracks, as library-relative paths, in Navidrome's order.

    The internal API is the only one that gives a real path: Subsonic's is
    built from tags and points at nothing on disk. ``id`` on these rows is the
    position in the playlist, which is what sorting on it means here.

    ``expected`` is the song count Navidrome itself reported. A short read --
    a dropped connection, a page boundary handled wrongly -- would look exactly
    like "the phone deleted the rest of the playlist", so a mismatch raises
    instead of returning a truncated list that a caller would write to disk.
    """
    rows: list[dict] = []
    start = 0
    while True:
        response = requests.get(
            f"{config.NAVIDROME_URL}/api/playlist/{playlist_id}/tracks",
            headers=_headers(),
            params={
                "_start": str(start),
                "_end": str(start + PAGE),
                "_sort": "id",
                "_order": "ASC",
            },
            timeout=TIMEOUT * 3,
        )
        response.raise_for_status()
        page = response.json() or []
        rows.extend(page)
        if len(page) < PAGE:
            break
        start += PAGE
        if start > expected + PAGE:
            break

    if len(rows) != expected:
        raise NavidromeUnavailable(
            f"Navidrome reported {expected} tracks for playlist {playlist_id} "
            f"but returned {len(rows)}; refusing to treat that as the playlist"
        )
    return [str(row.get("path") or "") for row in rows]
