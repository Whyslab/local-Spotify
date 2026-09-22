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
yet" takes two signals. When Navidrome ingests a file it stamps ``updatedAt``
within a few seconds of the file's mtime -- 5.6 s in the worst case measured
here -- and that stamp is remembered as the playlist's baseline. After that,
any move of ``updatedAt`` while the file stays the same is an edit that did
not come from us, however soon after our write it happened. The old rule --
"more than 60 s ahead of the file" -- never saw a phone edit made within a
minute of a laptop write, and the next laptop write erased it. The gap rule
stays for the first pass after a restart, when there is no baseline yet.

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

# The last updatedAt each playlist was reconciled against. Navidrome moves
# that stamp for reasons of its own -- Monday sat five days ahead of a file
# nobody had touched -- so without this every pass would re-read all 1075 of
# its tracks, forever, to conclude nothing had changed. In memory on purpose:
# losing it after a restart costs one extra read.
_SEEN: dict[str, str] = {}

# What Navidrome's updatedAt was once it had read our current file:
# name -> (file mtime, updatedAt). A later updatedAt for the same mtime is an
# edit made through the API -- the phone.
_BASE: dict[str, tuple[float, str]] = {}

# A track this recent may not be in Navidrome's database yet: it drops an
# .m3u line whose file it has not scanned. Such lines are never read as
# "removed on the phone".
FRESH_SECONDS = 7 * 24 * 3600

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
    stamp = str(entry.get("updatedAt") or "")
    updated = _remote_epoch(stamp)
    if updated is None:
        return False, "unreadable updatedAt"
    if _SEEN.get(name) == stamp:
        return False, "already reconciled at this revision"
    base = _BASE.get(name)
    if base and base[0] == mtime and base[1] != stamp:
        return True, "changed in Navidrome since it read the file"
    if updated - mtime <= QUIET_SECONDS:
        # Navidrome has read this very file (its stamp is not older than the
        # write) -- remember that as the baseline. Before it reads it, the
        # stamp is older than the mtime and means nothing yet.
        if updated >= mtime - 1:
            _BASE[name] = (mtime, stamp)
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
    # The file first: its revision then also covers a laptop write that lands
    # while the remote list is being fetched.
    current = playlists.read(name)
    local = [line.path for line in current.entries]
    merged, kept = _merged(name, entry, local)

    if merged == local:
        return {"playlist": name, "changed": False, "tracks": len(local)}

    playlists.write(name, merged, expected_revision=current.revision)
    logger.info("Playlist %r pulled back from Navidrome: %d tracks", name, len(merged))
    return {
        "playlist": name,
        "changed": True,
        "tracks": len(merged),
        "kept_missing": kept,
        "was": len(local),
    }


def _merged(name: str, entry: dict, local: list[str]) -> tuple[list[str], int]:
    """What the file should hold after taking Navidrome's order.

    Declines (raises) rather than guess: an empty remote list for a non-empty
    file, or a remote path this library does not know -- a Navidrome upgrade
    that changes how paths are written would otherwise rewrite every playlist
    with paths nothing can play.
    """
    remote = navidrome.remote_tracks(str(entry["id"]), int(entry.get("songCount") or 0))
    remote = [path for path in remote if path]
    if not remote and local:
        raise RuntimeError("Navidrome returned an empty playlist for a file that is not empty")

    known = {row["path"] for row in library.library_index()}
    stray = [path for path in remote if path not in known]
    if stray:
        raise RuntimeError(f"Navidrome lists {len(stray)} path(s) this library does not know")

    # Kept even though Navidrome does not list them: files missing from the
    # library (it cannot see them), and files too new for it to have scanned.
    # Taking its list as the whole truth would drop both.
    remote_set = set(remote)
    now = time.time()
    kept = [
        path
        for path in local
        if path not in remote_set and (path not in known or _is_fresh(path, now))
    ]
    return remote + kept, len(kept)


def _is_fresh(rel_path: str, now: float) -> bool:
    try:
        return now - library.library_track(rel_path).stat().st_mtime < FRESH_SECONDS
    except Exception:
        return False


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
                # The same merge the real pass would write, guards included.
                local = [line.path for line in playlists.read(name).entries]
                merged, _ = _merged(name, entry, local)
                row["changed"] = merged != local
                row["tracks"] = len(merged)
            # Only on success: a playlist that could not be read must be
            # looked at again rather than written off as reconciled.
            if not row.get("changed"):
                _SEEN[name] = str(entry.get("updatedAt"))
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


_CHECK_LOCK = threading.Lock()


def _loop() -> None:
    while not _stop.wait(POLL_SECONDS):
        try:
            # Deletes and renames that failed while Navidrome was down: retried
            # here, not only at the next service start.
            navidrome.drain()
            with _CHECK_LOCK:
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
    _stop.clear()
    thread = threading.Thread(target=_loop, name="playlist-sync", daemon=True)
    thread.start()
    logger.info("Playlist sync watching Navidrome every %ds", POLL_SECONDS)
    return thread


def stop() -> None:
    _stop.set()
