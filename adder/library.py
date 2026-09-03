"""The music library on disk: listing it, resolving paths into it, removing from it.

Everything here reads ``config.LIBRARY`` at call time rather than importing the
value, so redirecting the library in a test redirects it for this module too.
"""

import logging
import shutil
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from mutagen.mp4 import MP4

from . import config, runtime

logger = logging.getLogger(__name__)

# Every track in the library arrives through yt-dlp and is normalised to .m4a,
# so tags can be read with the MP4 parser directly. Accepting files from disk
# will widen this, and that needs a format-aware tag reader alongside it.
AUDIO_SUFFIXES = (".m4a",)


def library_track(rel_path: str) -> Path:
    """Resolve a library-relative path, refusing anything that escapes the library.

    The API listens on the LAN, so a caller must never be able to reach a file
    outside the music folder by sending ``../`` or an absolute path.
    """
    candidate = (config.LIBRARY / rel_path).resolve()
    root = config.LIBRARY.resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Path is outside the library")
    if not candidate.is_file() or candidate.suffix.lower() not in AUDIO_SUFFIXES:
        raise HTTPException(status_code=404, detail="Track not found")
    return candidate


LIBRARY_INDEX_TTL = 60
_LIBRARY_INDEX: dict[str, object] = {"at": 0.0, "rows": []}
_LIBRARY_INDEX_LOCK = threading.Lock()


def library_index() -> list[dict]:
    """Every track in the library with the tags the panel displays.

    Reading a thousand files takes the better part of a second, and the panel
    searches on every keystroke, so the parsed result is cached. The TTL covers
    changes made outside this process; anything this process does to the
    library calls invalidate_library_index() and takes effect at once.
    """
    with _LIBRARY_INDEX_LOCK:
        if time.time() - float(_LIBRARY_INDEX["at"]) < LIBRARY_INDEX_TTL:
            return list(_LIBRARY_INDEX["rows"])

        root = config.LIBRARY.resolve()
        rows = []
        for f in sorted(root.rglob("*.m4a")):
            try:
                parsed = MP4(f)
            except Exception:
                continue
            tags = parsed.tags or {}
            # Duration is needed by three separate things: the #EXTINF line of a
            # playlist, the search results that let you pick between two uploads
            # of the same song, and the duration window that matches a playlist
            # entry to a YouTube result. Reading it here costs one already-open
            # file rather than a second pass over the library.
            duration = getattr(getattr(parsed, "info", None), "length", None)
            artist = " • ".join(tags.get("\xa9ART") or [])
            title = (tags.get("\xa9nam") or [f.stem])[0]
            album = (tags.get("\xa9alb") or [""])[0]
            track = tags.get("trkn") or []
            rows.append(
                {
                    "path": str(f.relative_to(root)),
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "track": track[0][0] if track else None,
                    "albumartist": (tags.get("aART") or [""])[0],
                    "duration": round(duration, 3) if duration else None,
                    "haystack": f"{artist} {title} {album}".lower(),
                }
            )

        _LIBRARY_INDEX.update({"at": time.time(), "rows": rows})
        return list(rows)


def invalidate_library_index() -> None:
    _LIBRARY_INDEX["at"] = 0.0


def library_counts() -> tuple[int, int]:
    """Track and album totals for the panel header, off the shared index."""
    try:
        rows = library_index()
    except Exception:
        return 0, 0
    albums = {(r["album"], r["albumartist"]) for r in rows if r["album"]}
    return len(rows), len(albums)


def delete_track(rel_path: str) -> dict:
    """Remove a track from the library.

    The file is moved to a trash folder rather than unlinked, so a mistaken tap
    on a phone stays recoverable. Navidrome's watcher notices the file is gone
    and drops it from the library on its own.
    """
    runtime.guard_real_library(config.LIBRARY, "delete a track")
    target = library_track(rel_path)
    destination = runtime.TRASH_DIR / rel_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamped = f"{destination.stem}-{int(time.time())}{destination.suffix}"
        destination = destination.with_name(stamped)
    shutil.move(str(target), str(destination))

    # Leave no empty artist/album folders behind.
    for parent in target.parents:
        if parent == config.LIBRARY.resolve():
            break
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        else:
            break

    invalidate_library_index()
    logger.info("Deleted %s -> %s", rel_path, destination)
    return {"deleted": rel_path, "trash": str(destination)}
