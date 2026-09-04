"""Bringing an edit made on the phone back into the .m3u file.

Everything else in this project pushes one way: the file is written here, and
Navidrome mirrors it a few seconds later. That covers the laptop editing a
playlist and the phone seeing it. It does not cover the phone editing one, and
what happens then is worse than "nothing happens".

Measured against Navidrome 0.63.2 and Amperfy 2.1.x on 2026-09-03:

* A reorder sent from the phone (Subsonic ``updatePlaylist``) IS applied --
  Navidrome answers 200 and its database really does hold the new order. The
  older note in this repository saying it is ignored was wrong.
* The .m3u is not touched. File and database now disagree.
* The moment anything changes that file's mtime, Navidrome's watcher re-reads
  it and the phone's edit is gone. No error, no trace.

So the phone can edit, and the edit survives exactly until the next write from
the laptop. This module closes that loop: it notices a playlist whose database
order no longer matches its file, and writes the file to match. After that the
watcher re-reads the file, finds the same order, and the two agree for good.

Telling "the phone edited this" apart from "Navidrome has not read our write
yet" is done with one cheap signal. When Navidrome ingests a file it stamps
``updatedAt`` within a few seconds of the file's mtime -- 5.6 s in the worst
case measured here. When a client edits through the API, ``updatedAt`` moves
and the mtime does not. A gap wider than QUIET_SECONDS is therefore an edit
that did not come from us, and nothing else, with a ten-fold margin.

What this module will not do is guess. If Navidrome returns fewer tracks than
it says the playlist has, or the playlist has no file, or the remote list comes
back empty for a non-empty file, it declines and says why -- a wrong guess here
overwrites a playlist of a thousand tracks.
"""

import logging
import threading
import time
from datetime import datetime

from . import library, navidrome, playlists

logger = logging.getLogger(__name__)

# How far Navidrome's updatedAt has to be ahead of the file's mtime before the
# difference is read as an edit from somewhere else. Ingest was measured at
# 5.6 s; this is an order of magnitude more.
QUIET_SECONDS = 60

# How often the background loop looks. One cheap request per pass -- the
# per-playlist track fetch only happens for a playlist that already looks
# edited.
POLL_SECONDS = 30

_LAST: dict[str, object] = {"at": 0.0, "result": None, "error": None}
_LOCK = threading.Lock()
_stop = threading.Event()


def _mtime(name: str) -> float | None:
    try:
        return playlists.playlist_path(name).stat().st_mtime
    except Exception:
        return None


def _remote_epoch(value: str) -> float | None:
    """Navidrome's RFC3339 timestamps, which carry more than six fractional digits.

    ``datetime.fromisoformat`` rejects nine, and this is the only thing the
    value is used for, so the fraction is simply dropped.
    """
    if not value:
        return None
    head, _, rest = value.partition(".")
    zone = ""
    for index, char in enumerate(rest):
        if char in "+-Z":
            zone = rest[index:]
            break
    try:
        return datetime.fromisoformat(head + (zone if zone != "Z" else "+00:00")).timestamp()
    except ValueError:
        return None


def _diverged(entry: dict) -> tuple[bool, str]:
    """Does this Navidrome playlist look edited somewhere other than here?"""
    if not entry.get("sync") or not entry.get("path"):
        return False, "not backed by a file"
    name = entry.get("name") or ""
    mtime = _mtime(name)
    if mtime is None:
        return False, "no file on disk"
    updated = _remote_epoch(str(entry.get("updatedAt") or ""))
    if updated is None:
        return False, "unreadable updatedAt"
    if updated - mtime <= QUIET_SECONDS:
        return False, "in step with the file"
    return True, f"Navidrome is {int(updated - mtime)}s ahead of the file"


def pull_back(name: str, entry: dict) -> dict:
    """Write Navidrome's order for one playlist into its .m3u.

    Lines whose files are missing from the library are kept. Navidrome cannot
    see them -- it silently drops an .m3u line pointing at a file that is not
    there -- so taking its list as the whole truth would delete every track
    that happens to be missing at that moment. They keep their relative order
    and go to the end.
    """
    remote = navidrome.remote_tracks(str(entry["id"]), int(entry.get("songCount") or 0))
    remote = [path for path in remote if path]

    current = playlists.read(name)
    local = [line.path for line in current.entries]

    if not remote and local:
        raise RuntimeError("Navidrome returned an empty playlist for a file that is not empty")

    known = {row["path"] for row in library.library_index()}
    invisible = [path for path in local if path not in known]
    merged = remote + invisible

    if merged == local:
        return {"playlist": name, "changed": False, "tracks": len(local)}

    playlists.write(name, merged, expected_revision=current.revision)
    logger.info(
        "Playlist %r pulled back from Navidrome: %d tracks (%d of them invisible to it)",
        name,
        len(merged),
        len(invisible),
    )
    return {
        "playlist": name,
        "changed": True,
        "tracks": len(merged),
        "kept_missing": len(invisible),
        "was": len(local),
    }


def check(apply: bool = True) -> dict:
    """One pass over every playlist Navidrome holds.

    With ``apply`` false it reports what it would write and writes nothing,
    which is what the status endpoint uses.
    """
    if not navidrome.configured():
        return {"navidrome": "not configured", "playlists": []}

    entries = navidrome.playlists()
    report: list[dict] = []
    for entry in entries:
        diverged, why = _diverged(entry)
        name = entry.get("name") or ""
        if not diverged:
            continue
        row = {"playlist": name, "reason": why}
        try:
            if apply:
                row.update(pull_back(name, entry))
            else:
                remote = [
                    path
                    for path in navidrome.remote_tracks(
                        str(entry["id"]), int(entry.get("songCount") or 0)
                    )
                    if path
                ]
                local = [line.path for line in playlists.read(name).entries]
                known = {track["path"] for track in library.library_index()}
                merged = remote + [path for path in local if path not in known]
                row["changed"] = merged != local
                row["tracks"] = len(merged)
        except Exception as exc:  # noqa: BLE001 -- reported, never raised into the loop
            row["error"] = str(exc)[:300]
            logger.warning("Could not pull %r back from Navidrome: %s", name, exc)
        report.append(row)

    result = {
        "navidrome": "ok",
        "checked": len(entries),
        "playlists": report,
        "applied": apply,
    }
    with _LOCK:
        _LAST.update({"at": time.time(), "result": result, "error": None})
    return result


def status() -> dict:
    """What the last pass found, for the panel that shows sync state."""
    with _LOCK:
        last = dict(_LAST)
    age = time.time() - float(last["at"] or 0)
    return {
        "configured": navidrome.configured(),
        "queued": navidrome.pending_count(),
        "last_check_seconds_ago": int(age) if last["at"] else None,
        "last_result": last["result"],
        "last_error": last["error"],
        "poll_seconds": POLL_SECONDS,
    }


def _loop() -> None:
    while not _stop.wait(POLL_SECONDS):
        try:
            check(apply=True)
        except Exception as exc:  # noqa: BLE001 -- a down Navidrome must not kill the thread
            with _LOCK:
                _LAST.update({"at": time.time(), "error": str(exc)[:300]})
            logger.info("Playlist sync pass failed: %s", exc)


def start() -> threading.Thread | None:
    """Run the pull-back loop in the background, if Navidrome is configured."""
    if not navidrome.configured():
        logger.info("Playlist sync not started: Navidrome is not configured")
        return None
    thread = threading.Thread(target=_loop, name="playlist-sync", daemon=True)
    thread.start()
    logger.info("Playlist sync watching Navidrome every %ds", POLL_SECONDS)
    return thread


def stop() -> None:
    _stop.set()
