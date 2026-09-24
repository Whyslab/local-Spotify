"""The music library on disk: listing it, resolving paths into it, removing from it.

Everything here reads ``config.LIBRARY`` at call time rather than importing the
value, so redirecting the library in a test redirects it for this module too.
"""

import logging
import shutil
import threading
import time
from pathlib import Path

import mutagen
from fastapi import HTTPException
from mutagen.mp4 import MP4

from . import config, runtime

logger = logging.getLogger(__name__)

# Anything arriving through yt-dlp is normalised to .m4a. Files imported from
# disk are kept in the format they came in: re-encoding lossy audio into
# another lossy format to make the folder tidy costs quality and buys nothing,
# since Navidrome serves all of these already.
AUDIO_SUFFIXES = (".m4a", ".mp3", ".flac", ".opus", ".ogg")


def read_tags(path: Path) -> dict:
    """Artist, title, album, track number and duration, whatever the container.

    MP4 keys are atoms (\xa9nam and friends); everything else answers to the
    common names mutagen exposes with easy=True. One function so callers do not
    have to care which one they are holding.
    """
    suffix = path.suffix.lower()
    if suffix == ".m4a":
        parsed = MP4(path)
        tags = parsed.tags or {}
        track = tags.get("trkn") or []
        return {
            "artist": " \u2022 ".join(tags.get("\xa9ART") or []),
            "title": (tags.get("\xa9nam") or [path.stem])[0],
            "album": (tags.get("\xa9alb") or [""])[0],
            "albumartist": (tags.get("aART") or [""])[0],
            "track": track[0][0] if track else None,
            "duration": getattr(getattr(parsed, "info", None), "length", None),
        }

    parsed = mutagen.File(path, easy=True)
    if parsed is None:
        raise ValueError(f"unreadable audio file: {path}")
    tags = parsed.tags or {}

    def first(key: str) -> str:
        value = tags.get(key) or []
        return value[0] if value else ""

    number = first("tracknumber")
    try:
        # "7" and "7/12" both mean track seven.
        track_number = int(str(number).split("/")[0]) if number else None
    except ValueError:
        track_number = None

    return {
        "artist": " \u2022 ".join(tags.get("artist") or []),
        "title": first("title") or path.stem,
        "album": first("album"),
        "albumartist": first("albumartist"),
        "track": track_number,
        "duration": getattr(getattr(parsed, "info", None), "length", None),
    }


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
# Parsed row per file, keyed by path and valid while (mtime_ns, size) match.
_FILE_ROWS: dict[str, tuple[tuple[int, int], dict]] = {}
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
        # is_file() matters here: an artist folder can be named after a file.
        # This library has one called "nyan.mp3", and a glob for *.mp3 finds it.
        files = sorted(
            p for suffix in AUDIO_SUFFIXES for p in root.rglob(f"*{suffix}") if p.is_file()
        )
        seen = set()
        for f in files:
            try:
                stat = f.stat()
            except OSError:
                continue
            key = str(f)
            seen.add(key)
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = _FILE_ROWS.get(key)
            if cached and cached[0] == stamp:
                # Unchanged since the last pass: reading its tags again is what
                # made every rebuild (each add, each delete, /health once a
                # minute) cost a full pass over the library.
                rows.append(cached[1])
                continue
            try:
                # Duration comes from here too. Three things need it -- the
                # #EXTINF line of a playlist, the search results that let you
                # choose between two uploads of the same song, and the duration
                # window that matches a playlist entry to a YouTube result --
                # and reading it now costs one already-open file rather than a
                # second pass over the library.
                meta = read_tags(f)
                # stat — тоже здесь: файл, удалённый посреди перестройки,
                # иначе ронял весь список ошибкой 500.
                added = int(stat.st_mtime)
            except Exception:
                continue
            artist, title, album = meta["artist"], meta["title"], meta["album"]
            rows.append(
                {
                    "path": str(f.relative_to(root)),
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "track": meta["track"],
                    "albumartist": meta["albumartist"],
                    "duration": round(meta["duration"], 3) if meta["duration"] else None,
                    # Когда трек появился здесь. Своей даты добавления у файла
                    # нет, а тегам верить нельзя: год издания к «когда я это
                    # скачал» отношения не имеет. mtime ставится в момент, когда
                    # ингест дописывает теги, — это и есть «появился в фонотеке».
                    # Сдвигается при перетегировании, и это честнее, чем ничего:
                    # иначе свежие треки не отличить от собранных в августе.
                    "added": added,
                    "haystack": f"{artist} {title} {album}".lower(),
                }
            )
            _FILE_ROWS[key] = (stamp, rows[-1])

        for gone in set(_FILE_ROWS) - seen:
            del _FILE_ROWS[gone]

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
    # От проверенного пути, а не от присланной строки: «A/../../x» или
    # абсолютный путь внутри фонотеки уводили файл мимо корзины.
    relative = target.relative_to(config.LIBRARY.resolve())
    destination = runtime.TRASH_DIR / relative
    with runtime.FILE_LOCK:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stamped = f"{destination.stem}-{time.time_ns()}{destination.suffix}"
            destination = destination.with_name(stamped)
        shutil.move(str(target), str(destination))

        # Leave no empty artist/album folders behind. Under the same lock as
        # the move into the library, so a track being added for this artist
        # does not find its folder gone.
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


def embedded_cover(path: Path) -> tuple[bytes, str] | None:
    """The picture inside an audio file, if it has one.

    Each format hides it somewhere different, exactly as adder.ingest.write_tags
    puts it there: an MP4 ``covr`` atom, an ID3 APIC frame, a FLAC picture
    block, a base64 block in an Ogg comment. Returns the bytes and their media
    type, or None -- a track with no artwork is ordinary, not an error.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".m4a":
            from mutagen.mp4 import MP4, MP4Cover

            covers = MP4(path).get("covr") or []
            if covers:
                art = covers[0]
                fmt = getattr(art, "imageformat", MP4Cover.FORMAT_JPEG)
                return bytes(art), "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
            return None

        if suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError

            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return None
            frames = tags.getall("APIC")
            if frames:
                return frames[0].data, frames[0].mime or "image/jpeg"
            return None

        if suffix == ".flac":
            from mutagen.flac import FLAC

            pictures = FLAC(path).pictures
            if pictures:
                return pictures[0].data, pictures[0].mime or "image/jpeg"
            return None

        if suffix in (".opus", ".ogg"):
            import base64

            from mutagen.flac import Picture
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis

            audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
            blocks = audio.get("metadata_block_picture") or []
            if blocks:
                picture = Picture(base64.b64decode(blocks[0]))
                return picture.data, picture.mime or "image/jpeg"
            return None
    except Exception as exc:  # noqa: BLE001 -- a broken tag is not a broken library
        logger.debug("No cover read from %s: %s", path, exc)
    return None
