"""Playlists as .m3u files in the library root.

The file is the source of truth, not Navidrome's database. That is not a
preference: Navidrome silently ignores a reorder issued against a playlist it
imported from a file -- the API answers 200 and nothing changes -- while it
happily re-reads a rewritten file within about ten seconds. Writing the file is
the only way to move a track.

Two consequences shape this module.

A line is identified by its index, never by its path. Monday.m3u holds 1075
entries and only 1056 distinct paths: nineteen tracks appear twice. Ordering by
path would be ambiguous for those, and a drag would land the wrong one.

Every write is atomic and versioned. A crash between opening and writing would
leave an empty file, and Navidrome would faithfully mirror that -- a playlist of
1075 tracks gone, with no copy anywhere.
"""

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from . import config, library, runtime

logger = logging.getLogger(__name__)

SUFFIX = ".m3u"
HISTORY_DIR = runtime.PROJECT / "playlist-history"
HISTORY_KEEP = 20

# Playlist names arrive in a URL path and become file names. Tracks have
# library_track() to keep them inside the library; playlists need the same,
# because the service listens on the LAN.
_NAME_FORBIDDEN = re.compile(r"[/\\\x00-\x1f]")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def safe_name(name: str) -> str:
    """Validate a playlist name that is about to become a file name."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Playlist name is empty")
    if len(cleaned) > 120:
        raise HTTPException(status_code=400, detail="Playlist name is too long")
    if _NAME_FORBIDDEN.search(cleaned) or cleaned in {".", ".."} or cleaned.startswith("."):
        raise HTTPException(status_code=400, detail="Playlist name contains illegal characters")
    return cleaned


def playlist_path(name: str) -> Path:
    """Resolve a playlist name to its file, refusing anything outside the library."""
    candidate = (config.LIBRARY / f"{safe_name(name)}{SUFFIX}").resolve()
    root = config.LIBRARY.resolve()
    if not candidate.is_relative_to(root) or candidate.parent != root:
        raise HTTPException(status_code=400, detail="Playlist path is outside the library")
    return candidate


@dataclass(frozen=True)
class Entry:
    """One line of a playlist. ``index`` is its identity for the whole API."""

    index: int
    path: str
    title: str
    duration: int


@dataclass(frozen=True)
class Playlist:
    name: str
    entries: list[Entry]
    revision: str


def revision_of(path: Path) -> str:
    """A short hash of the file as it is on disk right now.

    Handed out with every read and demanded back on every write, so an edit
    made against a stale view is refused instead of silently overwriting an
    edit made from the other device.
    """
    if not path.exists():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def parse(text: str) -> list[str]:
    """The track paths of an .m3u, in order, keeping duplicates.

    #EXTINF lines are metadata for players; the paths are what matters, and a
    repeated path is a repeated entry rather than a mistake to collapse.
    """
    paths = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(line)
    return paths


def render(paths: list[str]) -> str:
    """Build an .m3u, with #EXTINF taken from the library index."""
    by_path = {row["path"]: row for row in library.library_index()}
    out = ["#EXTM3U"]
    for rel in paths:
        row = by_path.get(rel)
        if row:
            seconds = int(row["duration"]) if row.get("duration") else -1
            artist = row.get("artist") or ""
            title = row.get("title") or Path(rel).stem
            label = f"{artist} - {title}" if artist else title
        else:
            # A path the index does not know: still write it out rather than
            # dropping the line, so a track that is temporarily missing does
            # not quietly disappear from the playlist.
            seconds, label = -1, Path(rel).stem
        out.append(f"#EXTINF:{seconds},{label}")
        out.append(rel)
    return "\n".join(out) + "\n"


def read(name: str) -> Playlist:
    path = playlist_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Playlist not found")
    text = path.read_text(encoding="utf-8")
    by_path = {row["path"]: row for row in library.library_index()}
    entries = []
    for index, rel in enumerate(parse(text)):
        row = by_path.get(rel, {})
        artist, title = row.get("artist") or "", row.get("title") or Path(rel).stem
        entries.append(
            Entry(
                index=index,
                path=rel,
                title=f"{artist} - {title}" if artist else title,
                duration=int(row["duration"]) if row.get("duration") else -1,
            )
        )
    return Playlist(name=name, entries=entries, revision=revision_of(path))


def _archive(path: Path) -> None:
    """Keep the previous contents before overwriting them."""
    if not path.exists():
        return
    folder = HISTORY_DIR / path.stem
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, folder / f"{int(time.time() * 1000)}{SUFFIX}")
    versions = sorted(folder.glob(f"*{SUFFIX}"), reverse=True)
    for stale in versions[HISTORY_KEEP:]:
        stale.unlink(missing_ok=True)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory, then rename over.

    fsync on both the file and the directory: without the first the rename can
    land ahead of the data, without the second the rename itself can be lost.
    Navidrome re-reads this file on a timer and would mirror whatever it finds.
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def write(name: str, paths: list[str], expected_revision: str | None) -> Playlist:
    """Replace the contents of a playlist with ``paths``, in that order."""
    path = playlist_path(name)
    with _lock_for(name):
        current = revision_of(path)
        if expected_revision is not None and expected_revision != current:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Playlist changed since it was read. Re-read it and reapply the edit; "
                    f"expected revision {expected_revision}, on disk {current}."
                ),
            )
        _archive(path)
        _atomic_write(path, render(paths))
        logger.info("Playlist %r written: %d entries", name, len(paths))
    return read(name)


def create(name: str, paths: list[str] | None = None) -> Playlist:
    path = playlist_path(name)
    if path.exists():
        raise HTTPException(status_code=409, detail="Playlist already exists")
    with _lock_for(name):
        _atomic_write(path, render(paths or []))
    logger.info("Playlist %r created", name)
    return read(name)


def rename(name: str, new_name: str) -> Playlist:
    """Rename the file. The caller is responsible for Navidrome's side of it.

    Navidrome treats the new file as a new playlist and leaves the old one
    behind, so renaming is a file move plus a delete against its API plus a
    re-upload of the cover -- see adder.navidrome.
    """
    source, target = playlist_path(name), playlist_path(new_name)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Playlist not found")
    if target.exists():
        raise HTTPException(status_code=409, detail="A playlist with that name already exists")
    with _lock_for(name), _lock_for(new_name):
        os.replace(source, target)
    logger.info("Playlist %r renamed to %r", name, new_name)
    return read(new_name)


def delete(name: str) -> dict:
    """Move the playlist file to trash rather than unlinking it."""
    path = playlist_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Playlist not found")
    runtime.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    destination = runtime.TRASH_DIR / path.name
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}-{int(time.time())}{SUFFIX}")
    with _lock_for(name):
        shutil.move(str(path), str(destination))
    logger.info("Playlist %r deleted -> %s", name, destination)
    return {"deleted": name, "trash": str(destination)}


def listing() -> list[dict]:
    """Every playlist in the library root, with its size and revision."""
    root = config.LIBRARY
    if not root.exists():
        return []
    out = []
    for path in sorted(root.glob(f"*{SUFFIX}")):
        try:
            count = len(parse(path.read_text(encoding="utf-8")))
        except OSError:
            continue
        out.append(
            {
                "name": path.stem,
                "tracks": count,
                "revision": revision_of(path),
                "updated_at": int(path.stat().st_mtime),
            }
        )
    return out
