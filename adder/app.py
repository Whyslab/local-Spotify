"""YouTube -> Navidrome: веб-интерфейс + фоновые воркеры."""

import hashlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mutagen.mp4 import MP4, MP4Cover
from pydantic import BaseModel

# Import unified configuration
from . import enrich
from .config import (
    API_TOKEN,
    COOKIES_FROM_BROWSER,
    LIBRARY,
    MAX_LINKS_PER_REQUEST,
    MAX_QUEUE_SIZE,
    MAX_RETRIES,
    MAX_WORKERS,
    MIN_FREE_SPACE_MB,
    PRESERVE_FEAT_ARTISTS,
    RETRY_BACKOFF_BASE,
    SHUTDOWN_TIMEOUT,
    TMP_TTL_HOURS,
)

PROJECT = Path(__file__).resolve().parent
TMP_DIR = PROJECT / "tmp"
DB_PATH = PROJECT / "adder.db"

# Problem #29: Temporary directory configuration
TMP_TTL_SECONDS = TMP_TTL_HOURS * 3600


TASK_QUEUE: queue.Queue = queue.Queue()
# Lock for thread-safe file operations and duplicate checking
FILE_LOCK = threading.Lock()
# Set of URLs currently being processed to prevent duplicates
PROCESSING_URLS: set = set()


# Problem #25: Structured logging
class TaskIdFormatter(logging.Formatter):
    """Formatter that tolerates log records without task_id."""

    def format(self, record):
        if not hasattr(record, "task_id"):
            record.task_id = "-"
        return super().format(record)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [task=%(task_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Apply the tolerant formatter to the root handlers so third-party
# libraries (httpx, uvicorn, etc.) cannot trigger KeyError.
for handler in logging.getLogger().handlers:
    handler.setFormatter(
        TaskIdFormatter("[%(asctime)s] [task=%(task_id)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )

logger = logging.getLogger(__name__)

# Problem #19: API Token authentication
security = HTTPBearer(auto_error=False)


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> bool:
    """Verify API token (Problem #19).

    config.py refuses to start the app at all if API_TOKEN is unset, so
    there is no "no token configured" case to allow through here.
    secrets.compare_digest avoids leaking the token via a timing side
    channel on the comparison.
    """
    if credentials is None or not secrets.compare_digest(credentials.credentials, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True


# Problem #22: Graceful shutdown state
shutdown_event = threading.Event()
active_workers = []


class ShutdownRequested(Exception):
    """Raised when a task is interrupted because the service is shutting down."""


# ---------------- SQLite ----------------
def db_exec(sql: str, params=()):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur
    finally:
        con.close()


def db_query(sql: str, params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def db_init():
    db_exec("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, status TEXT, artist TEXT, title TEXT, error TEXT,
        error_type TEXT, retry_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now','localtime')))""")
    # Add unique constraint on url to prevent duplicates
    try:
        db_exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_url ON tasks(url)")
    except sqlite3.Error as e:
        logger.warning(
            f"Could not create task URL unique index: {e}",
            extra={"task_id": "system"},
        )


def task_update(tid: int, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    db_exec(
        f"UPDATE tasks SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
        (*fields.values(), tid),
    )


def recover_queued_tasks():
    """Recovery tasks after restart (Problem #6)."""
    # Find all tasks that were not completed
    queued = db_query(
        "SELECT id, url, status FROM tasks WHERE status IN ('queued', 'downloading', 'tagging')"
    )
    recovered = 0
    for task in queued:
        # Reset interrupted tasks to queued
        if task["status"] in ("downloading", "tagging"):
            task_update(task["id"], status="queued")
        # Add to queue (avoiding duplicates)
        with FILE_LOCK:
            if task["url"] not in PROCESSING_URLS:
                TASK_QUEUE.put((task["id"], task["url"]))
                PROCESSING_URLS.add(task["url"])
                recovered += 1
    if recovered > 0:
        logger.info(
            f"Recovered {recovered} tasks from previous session", extra={"task_id": "system"}
        )


# ---------------- Текст / метаданные ----------------
def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    name = re.sub(r"[\[\]'\"]", "", str(name))
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


JUNK = [
    r"official\s+(music\s+)?(video|audio|lyric\s+video|clip)",
    r"official\s+(video|audio)",
    r"(lyric(s)?\s+video|visuali[sz]er|music\s+video)",
    r"премьера(\s+(трека|клипа))?",
    r"текст\s+песни",
]

# Version keywords that should be preserved in metadata (Problem #13)
VERSION_KEYWORDS = [
    r"\b(live|remix|acoustic|radio\s+edit|remastered|deluxe|explicit|clean)\b",
    r"\((live|remix|acoustic|radio\s+edit|remastered|deluxe|explicit|clean)[^)]*\)",
    r"\[(live|remix|acoustic|radio\s+edit|remastered|deluxe|explicit|clean)[^\]]*\]",
]


def clean_title(s: str, for_filename: bool = True) -> str:
    """Clean YouTube title for filesystem or metadata use."""

    for pattern in JUNK:
        s = re.sub(pattern, " ", s, flags=re.IGNORECASE)

    # Remove empty brackets left behind after junk removal.
    # Example: "Get Lucky (Official Video)" -> "Get Lucky".
    s = re.sub(r"\(\s*\)", " ", s)
    s = re.sub(r"\[\s*\]", " ", s)

    # For filenames, remove version information.
    # Metadata keeps version information such as "(Live)".
    if for_filename:
        for pattern in VERSION_KEYWORDS:
            s = re.sub(pattern, " ", s, flags=re.IGNORECASE)

        # Version removal can leave empty brackets.
        s = re.sub(r"\(\s*\)", " ", s)
        s = re.sub(r"\[\s*\]", " ", s)

    # Collapse whitespace.
    s = re.sub(r"\s{2,}", " ", s)

    # Only strip separators from the outside.
    # Do NOT strip parentheses/brackets because they can be meaningful
    # metadata, e.g. "Song (Live)".
    s = s.strip(" -–—|_,:")

    return s or "Unknown"


def extract_version_info(original_title: str) -> str:
    """Extract version information from original title (Problem #13)."""
    versions = []
    for pattern in VERSION_KEYWORDS:
        matches = re.findall(pattern, original_title, flags=re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                # Multiple groups in pattern, take first non-empty
                v = next((m for m in match if m), None)
                if v:
                    versions.append(v)
            elif match:
                versions.append(match)
    return " ".join(versions) if versions else ""


def split_artist_title(meta: dict):
    artist = meta.get("artist") or meta.get("creator") or ""
    title = meta.get("track") or meta.get("title") or "Unknown"
    if not artist and " - " in title:
        artist, title = title.split(" - ", 1)
    if not artist:
        artist = meta.get("uploader", "Unknown Artist")

    # Problem #12: Preserve full artist metadata
    full_artist = artist.strip()

    # For filesystem, use primary artist only (safe naming)
    if not PRESERVE_FEAT_ARTISTS:
        fs_artist = artist.split(",")[0].split(" feat")[0].split(" ft")[0]
    else:
        # Keep full artist string but sanitize for filesystem
        fs_artist = artist

    return (
        sanitize_filename(fs_artist),
        sanitize_filename(clean_title(title, for_filename=True)),
        full_artist,
        clean_title(title, for_filename=False),
    )


# ---------------- Сеть / yt-dlp ----------------
def validate_url(url: str) -> tuple[bool, str]:
    """Validate that a URL points to a supported YouTube host.

    Returns:
        (is_valid, error_message)
    """
    if not url or not url.strip():
        return False, "Empty URL"

    url = url.strip()

    # Max length check
    if len(url) > 2048:
        return False, "URL too long (max 2048 characters)"

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"

    # Check scheme
    if parsed.scheme not in ("http", "https"):
        return False, "URL must use http or https scheme"

    # Check hostname exists
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        return False, "URL must have a hostname"

    # Only YouTube URLs are supported.
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }

    if hostname not in allowed_hosts:
        return False, "URL must be a YouTube URL"

    return True, ""


def canonicalize_youtube_url(url: str) -> str:
    """Return one canonical URL for a supported YouTube video URL.

    The function normalizes different YouTube URL forms to:

        https://www.youtube.com/watch?v=VIDEO_ID

    It intentionally does not verify whether the video actually exists.
    That is the responsibility of yt-dlp during task processing.
    """
    from urllib.parse import parse_qs

    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")

    if hostname in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        if parsed.path != "/watch":
            raise ValueError("YouTube URL must use /watch?v=VIDEO_ID")

        video_id = parse_qs(parsed.query).get("v", [None])[0]

    elif hostname in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.lstrip("/").split("/", 1)[0]

    else:
        raise ValueError("URL must be a YouTube URL")

    if not video_id:
        raise ValueError("YouTube URL is missing video ID")

    # YouTube video IDs use URL-safe characters. We keep this check
    # deliberately independent of yt-dlp/existence validation so unit
    # tests can use synthetic IDs such as "abc123".
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        raise ValueError("Invalid YouTube video ID")

    return f"https://www.youtube.com/watch?v={video_id}"


def run_yt_dlp(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run yt-dlp with timeout and shutdown-aware subprocess handling.

    The subprocess is placed in its own process group so that yt-dlp and
    children such as ffmpeg can be terminated together during shutdown.

    stdout and stderr are drained continuously in non-blocking mode to
    prevent pipe-buffer deadlocks when yt-dlp produces a large amount
    of output.
    """
    import selectors

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )

    selector = selectors.DefaultSelector()

    if process.stdout is not None:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")

    if process.stderr is not None:
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    start_time = time.monotonic()

    def terminate_process() -> None:
        """Terminate the entire yt-dlp process group."""
        if process.poll() is not None:
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def drain_pipes() -> None:
        """Drain all currently available data without blocking."""
        for key in list(selector.get_map().values()):
            stream = key.fileobj

            while True:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    break
                except OSError:
                    with suppress(Exception):
                        selector.unregister(stream)
                    break

                if not chunk:
                    with suppress(Exception):
                        selector.unregister(stream)
                    break

                if key.data == "stdout":
                    stdout_chunks.append(chunk)
                else:
                    stderr_chunks.append(chunk)

    try:
        while True:
            events = selector.select(timeout=0.25)

            for key, _ in events:
                stream = key.fileobj

                while True:
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        break
                    except OSError:
                        with suppress(Exception):
                            selector.unregister(stream)
                        break

                    if not chunk:
                        with suppress(Exception):
                            selector.unregister(stream)
                        break

                    if key.data == "stdout":
                        stdout_chunks.append(chunk)
                    else:
                        stderr_chunks.append(chunk)

            if process.poll() is not None:
                drain_pipes()

                # systemd sends SIGTERM to the whole service cgroup, so
                # yt-dlp may be terminated before the worker observes
                # shutdown_event itself. Treat that subprocess termination
                # as an intentional shutdown, not as a task failure.
                if shutdown_event.is_set():
                    raise ShutdownRequested()

                break

            if shutdown_event.is_set():
                logger.info(
                    "Stopping yt-dlp subprocess due to shutdown",
                    extra={"task_id": "system"},
                )
                terminate_process()
                drain_pipes()
                raise ShutdownRequested()

            if time.monotonic() - start_time >= timeout:
                terminate_process()
                drain_pipes()
                raise subprocess.TimeoutExpired(cmd, timeout)

        return subprocess.CompletedProcess(
            cmd,
            process.returncode,
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        )

    except BaseException:
        terminate_process()
        raise

    finally:
        selector.close()


def ytdlp_base() -> list[str]:
    """The yt-dlp invocation every call starts from.

    The cookies belong here rather than on the download alone: YouTube applies
    its bot check to the metadata request too, and that one runs first, so a
    gated video never reaches the download step to benefit from them.
    """
    command = [sys.executable, "-m", "yt_dlp"]
    if COOKIES_FROM_BROWSER:
        command += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    return command


def yt_meta(url: str) -> dict:
    p = run_yt_dlp(
        [*ytdlp_base(), "-J", "--no-playlist", url],
        timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    return json.loads(p.stdout)


def yt_download(url: str, vid: str) -> Path:
    command = [
        *ytdlp_base(),
        "-x",
        "--audio-format",
        "m4a",
        "--audio-quality",
        "0",
        "--no-playlist",
        "-o",
        str(TMP_DIR / f"{vid}.%(ext)s"),
        url,
    ]

    p = run_yt_dlp(command, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    target = TMP_DIR / f"{vid}.m4a"
    if not target.exists():
        found = list(TMP_DIR.glob(f"{vid}.*"))
        if not found:
            raise RuntimeError("Файл не найден после скачивания")
        target = found[0]
    return target


def get_hd_cover(artist: str, title: str):
    """Возвращает (bytes, fmt) или (None, None)."""
    try:
        params = {"term": f"{artist} {title}", "limit": 1, "entity": "song"}
        r = requests.get("https://itunes.apple.com/search", params=params, timeout=10)
        if r.ok and r.json().get("resultCount", 0) > 0:
            art = (
                r.json()["results"][0].get("artworkUrl100", "").replace("100x100bb", "3000x3000bb")
            )
            img = requests.get(art, timeout=15)
            if img.ok:
                return img.content, "jpg"
    except Exception:
        pass
    return None, None


def fetch_cover_url(url: str):
    """Download album art from a known-good URL (Deezer gives us one per album)."""
    try:
        img = requests.get(url, timeout=15)
        if img.ok and img.content:
            return img.content, ("png" if img.content.startswith(b"\x89PNG") else "jpg")
    except Exception:
        pass
    return None, None


def fetch_cover(artist: str, title: str, thumb_url: str | None):
    data, fmt = get_hd_cover(artist, title)
    if data:
        return data, fmt
    if thumb_url:  # fallback: превью YouTube
        try:
            img = requests.get(thumb_url, timeout=15)
            if img.ok:
                return img.content, ("png" if img.content.startswith(b"\x89PNG") else "jpg")
        except Exception:
            pass
    return None, None


def file_sha256(filepath: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of a file."""
    digest = hashlib.sha256()

    with filepath.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def find_duplicate_library_file(filepath: Path) -> Path | None:
    """Find an existing library M4A file with identical content.

    This is content-based duplicate detection. Filename differences,
    metadata differences, and directory differences do not matter.
    """
    if not filepath.exists():
        return None

    try:
        source_size = filepath.stat().st_size
    except OSError:
        return None

    source_hash = file_sha256(filepath)

    if not LIBRARY.exists():
        return None

    for candidate in LIBRARY.rglob("*.m4a"):
        try:
            if not candidate.is_file():
                continue

            if candidate == filepath:
                continue

            # Avoid hashing files with different sizes.
            if candidate.stat().st_size != source_size:
                continue

            if file_sha256(candidate) == source_hash:
                return candidate

        except (OSError, PermissionError) as exc:
            logger.warning(
                f"Could not inspect library file {candidate}: {exc}",
                extra={"task_id": "system"},
            )

    return None


def unique_path(base: Path) -> Path:
    p, n = base, 1
    while p.exists():
        p = base.with_name(f"{base.stem} ({n}){base.suffix}")
        n += 1
    return p


# ---------------- Воркер ----------------
def check_disk_space() -> tuple[bool, int]:
    """Check if there's enough free disk space (Problem #30).

    Returns:
        (has_space, free_mb)
    """
    try:
        import shutil

        stat = shutil.disk_usage(TMP_DIR)
        free_mb = stat.free // (1024 * 1024)
        return free_mb >= MIN_FREE_SPACE_MB, free_mb
    except Exception:
        return True, 0  # If we can't check, allow operation


def validate_m4a_integrity(filepath: Path) -> tuple[bool, str]:
    """Validate M4A file integrity (Problem #28).

    Returns:
        (is_valid, error_message)
    """
    if not filepath.exists():
        return False, "File does not exist"

    if filepath.stat().st_size == 0:
        return False, "File is empty"

    try:
        audio = MP4(filepath)
        # Check for audio stream
        if not audio.info or not hasattr(audio.info, "length") or audio.info.length <= 0:
            return False, "No valid audio stream found"
        return True, ""
    except Exception as e:
        return False, f"M4A validation failed: {str(e)[:100]}"


def cleanup_old_temp_files():
    """Clean up old temporary files on startup (Problem #29)."""
    if not TMP_DIR.exists():
        return

    current_time = time.time()
    cleaned = 0

    for f in TMP_DIR.glob("*"):
        try:
            # Don't delete files that are currently being processed
            mtime = f.stat().st_mtime
            age_seconds = current_time - mtime

            if age_seconds > TMP_TTL_SECONDS:
                f.unlink()
                cleaned += 1
                logger.info(f"Cleaned up old temp file: {f.name}", extra={"task_id": "system"})
        except OSError as e:
            logger.warning(
                f"Could not clean temp file {f.name}: {e}",
                extra={"task_id": "system"},
            )

    if cleaned > 0:
        logger.info(f"Cleaned {cleaned} old temp files", extra={"task_id": "system"})


# Errors worth trying again. Everything else is treated as permanent, so a
# genuinely broken link is not retried three times before giving up.
RETRYABLE_ERRORS = {"network_error", "download_error", "artwork_error"}


def classify_error(message: str) -> str:
    """
    Bucket an exception message so the retry logic can decide what is worth
    another attempt.

    Order matters. Transient causes are matched before the generic ones,
    because yt-dlp and httpx messages routinely mention the URL and the word
    "download" while describing a timeout — matching those first would classify
    a temporary network failure as permanent and skip the retry entirely.
    """
    text = message.lower()

    if "timeout" in text or "timed out" in text or "network" in text:
        return "network_error"
    if "connection" in text or "unreachable" in text or "temporarily" in text:
        return "network_error"
    if "http error 5" in text or "502" in text or "503" in text or "504" in text:
        return "network_error"

    if "disk" in text or "space" in text or "no space left" in text:
        return "filesystem_error"
    if "database" in text or "sqlite" in text:
        return "database_error"

    # Permanently wrong link, as opposed to one that merely failed to load.
    if "invalid url" in text or "unsupported url" in text or "malformed" in text:
        return "invalid_url"
    if "not found" in text or "unavailable" in text or "private video" in text:
        return "youtube_not_found"

    if "artwork" in text or "cover" in text:
        return "artwork_error"
    if "metadata" in text:
        return "metadata_error"
    if "download" in text:
        return "download_error"

    return "unknown_error"


def process(tid: int, url: str):
    tmp_file = None
    temp_path = None
    retry_count = 0
    last_error_type = None

    while retry_count == 0 or retry_count < MAX_RETRIES:
        try:
            # Problem #30: Check disk space before download
            has_space, free_mb = check_disk_space()
            if not has_space:
                raise RuntimeError(
                    f"Insufficient disk space: {free_mb}MB free, {MIN_FREE_SPACE_MB}MB required"
                )

            task_update(tid, status="downloading")
            meta = yt_meta(url)

            # Get artist/title for matching and metadata
            fs_artist, fs_title, full_artist, meta_title = split_artist_title(meta)

            # Problem #8 & #28: Download to temporary file first
            tmp_file = yt_download(url, meta["id"])
            temp_path = TMP_DIR / f"{meta['id']}_processing.m4a"

            # Validate downloaded file before processing (Problem #28)
            if not tmp_file.exists():
                raise RuntimeError("Downloaded file not found")

            is_valid, error_msg = validate_m4a_integrity(tmp_file)
            if not is_valid:
                raise RuntimeError(error_msg)

            # Move to temp processing location (not final library yet)
            shutil.move(str(tmp_file), str(temp_path))
            tmp_file = temp_path

            # Problem #12: Write full metadata
            task_update(tid, status="tagging", artist=full_artist, title=meta_title)

            # YouTube gives us a title and an uploader; Deezer gives us the album,
            # the track number and the real list of artists. Without this the track
            # lands in a nameless bucket with no position, which is what made every
            # album in the library read "Singles" in the first place.
            info, from_deezer = enrich.describe(full_artist, meta_title)
            logger.info(
                "Metadata for %r by %r: album=%r track=%s source=%s",
                meta_title, full_artist, info.album, info.track_number,
                "deezer" if from_deezer else "fallback",
            )

            cover, fmt = None, None
            if info.cover_url:
                cover, fmt = fetch_cover_url(info.cover_url)
            if not cover:
                cover, fmt = fetch_cover(fs_artist, fs_title, meta.get("thumbnail"))

            # Folder layout is left alone on purpose: Navidrome groups albums by
            # tags, not by directory, so moving files would buy nothing.
            target_dir = LIBRARY / fs_artist / "Singles"
            target_dir.mkdir(parents=True, exist_ok=True)
            base_target = target_dir / f"{fs_title}.m4a"

            # Problem #8: Process metadata on temp file BEFORE moving to library
            audio = MP4(tmp_file)
            audio["\xa9nam"] = [meta_title]  # Full title with version info
            audio["\xa9ART"] = info.artists  # one value per artist, so feats link to both
            audio["aART"] = [info.artists[0]]
            audio["\xa9alb"] = [info.album]
            if info.date:
                audio["\xa9day"] = [info.date]
            if info.track_number:
                audio["trkn"] = [(info.track_number, info.track_total)]
                audio["disk"] = [(info.disc_number, 1)]
            if cover:
                fmt_const = MP4Cover.FORMAT_PNG if fmt == "png" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(cover, imageformat=fmt_const)]
            audio.save()

            # Validate the processed file (Problem #28)
            audio_verify = MP4(tmp_file)
            if not audio_verify.get("\xa9nam"):
                raise RuntimeError("Metadata write failed verification")

            is_valid, error_msg = validate_m4a_integrity(tmp_file)
            if not is_valid:
                raise RuntimeError(f"Final validation failed: {error_msg}")

            # Content-based duplicate detection must happen while
            # holding the same lock as the final move. This prevents
            # concurrent workers from both accepting identical audio.
            with FILE_LOCK:
                duplicate = find_duplicate_library_file(tmp_file)

                if duplicate is not None:
                    logger.info(
                        f"Duplicate content detected; keeping existing file "
                        f"{duplicate} and discarding temporary file {tmp_file}",
                        extra={"task_id": tid},
                    )

                    tmp_file.unlink()
                    tmp_file = None

                    task_update(
                        tid,
                        status="done",
                        error="",
                        error_type="",
                    )
                    return

                # Same filename + different content is allowed.
                # Preserve the existing collision-safe naming behavior.
                final_target = unique_path(base_target)
                shutil.move(str(tmp_file), str(final_target))

            tmp_file = None  # Successfully moved, don't cleanup in finally
            invalidate_library_index()  # a new track must show up in search now

            # Problem #24: Clear error fields on success
            task_update(tid, status="done", error="", error_type="")
            return  # Success, exit retry loop

        except ShutdownRequested:
            logger.info(
                "Task interrupted by shutdown; returning task to queued state",
                extra={"task_id": tid},
            )

            task_update(
                tid,
                status="queued",
                error="",
                error_type="",
                retry_count=retry_count,
            )

            if tmp_file and tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as cleanup_error:
                    logger.warning(
                        f"Could not remove temporary file {tmp_file}: {cleanup_error}",
                        extra={"task_id": tid},
                    )

            with FILE_LOCK:
                PROCESSING_URLS.discard(url)

            return

        except Exception as e:
            error_str = str(e)[:300]

            last_error_type = classify_error(error_str)

            if last_error_type in RETRYABLE_ERRORS and retry_count < MAX_RETRIES - 1:
                retry_count += 1
                backoff_time = RETRY_BACKOFF_BASE**retry_count
                logger.warning(
                    f"Retry {retry_count}/{MAX_RETRIES} after {backoff_time}s: {error_str}",
                    extra={"task_id": tid},
                )

                # Allow SIGTERM/shutdown to interrupt retry backoff immediately.
                if shutdown_event.wait(backoff_time):
                    logger.info(
                        "Shutdown requested during retry backoff",
                        extra={"task_id": tid},
                    )

                    # The normal cleanup below the retry loop is skipped by
                    # this early return, so release the processing lock here.
                    with FILE_LOCK:
                        PROCESSING_URLS.discard(url)

                    return

                continue

            # Not retryable or max retries reached
            task_update(
                tid,
                status="error",
                error=error_str,
                error_type=last_error_type,
                retry_count=retry_count,
            )

            # Problem #8: Cleanup temp files on error
            if tmp_file and tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as e:
                    logger.warning(
                        f"Could not remove temporary file {tmp_file}: {e}",
                        extra={"task_id": tid},
                    )

            break  # Exit retry loop
    # Remove URL from processing set only after the entire task
    # (including all retry attempts) has finished.
    with FILE_LOCK:
        PROCESSING_URLS.discard(url)


def worker():
    while not shutdown_event.is_set():
        try:
            tid, url = TASK_QUEUE.get(timeout=1)
        except queue.Empty:
            continue

        try:
            if shutdown_event.is_set():
                task_update(
                    tid,
                    status="queued",
                    error="",
                    error_type="",
                )

                with FILE_LOCK:
                    PROCESSING_URLS.discard(url)

                return

            process(tid, url)
        finally:
            TASK_QUEUE.task_done()


# ---------------- Web ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize runtime state and gracefully stop workers."""
    PROJECT.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY.mkdir(parents=True, exist_ok=True)
    db_init()

    # Start background workers.
    # Each worker consumes tasks from the shared queue and processes them.
    shutdown_event.clear()
    active_workers.clear()

    # Recover tasks left unfinished by a previous process.
    recover_queued_tasks()

    # Remove stale temporary files from previous runs.
    cleanup_old_temp_files()

    for i in range(MAX_WORKERS):
        worker_thread = threading.Thread(
            target=worker,
            name=f"music-adder-worker-{i + 1}",
            daemon=True,
        )
        active_workers.append(worker_thread)
        worker_thread.start()

    logger.info(
        f"Started {len(active_workers)} worker(s)",
        extra={"task_id": "system"},
    )

    yield

    # Uvicorn handles SIGTERM and enters the lifespan shutdown phase.
    # Stop workers without blocking indefinitely.
    shutdown_event.set()

    if active_workers:
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for worker_thread in active_workers:
            remaining = max(0, deadline - time.monotonic())
            worker_thread.join(timeout=remaining)

        active_workers.clear()

    # Cleanup temporary files after workers have stopped.
    if TMP_DIR.exists():
        for f in TMP_DIR.glob("*"):
            try:
                f.unlink()
                logger.info(
                    f"Cleaned up temp file: {f.name}",
                    extra={"task_id": "system"},
                )
            except OSError as e:
                logger.warning(
                    f"Could not remove temp file during shutdown {f.name}: {e}",
                    extra={"task_id": "system"},
                )


app = FastAPI(lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT.parent / "web")),
    name="static",
)


class AddRequest(BaseModel):
    links: list[str]


@app.post("/api/add")
def add(req: AddRequest, authenticated: bool = Depends(verify_token)):
    """Add YouTube links to queue (Problem #19: API auth)."""
    # Problem #9: Check request limits
    if len(req.links) > MAX_LINKS_PER_REQUEST:
        raise HTTPException(
            status_code=400, detail=f"Too many links. Maximum {MAX_LINKS_PER_REQUEST} per request."
        )

    # Problem #9: Check queue size limit
    current_queue_size = TASK_QUEUE.qsize()
    if current_queue_size + len(req.links) > MAX_QUEUE_SIZE:
        raise HTTPException(
            status_code=429,
            detail=f"Queue full. Current: {current_queue_size}, Max: {MAX_QUEUE_SIZE}",
        )

    ids = []
    for link in req.links:
        link = link.strip()
        if not link:
            continue

        # Problem #10: Validate URL
        is_valid, error_msg = validate_url(link)
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Invalid URL: {error_msg}")

        # Normalize all supported YouTube URL forms to one canonical URL
        # before duplicate checks and database insertion.
        try:
            link = canonicalize_youtube_url(link)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid YouTube URL: {exc}",
            ) from exc

        # Problem #7: Check for duplicate URLs already in queue/processing
        with FILE_LOCK:
            if link in PROCESSING_URLS:
                continue  # Skip duplicate

            # Check if URL already exists in database.
            # Failed tasks can be explicitly retried by re-submitting the URL.
            existing = db_query(
                "SELECT id, status FROM tasks WHERE url = ?",
                (link,),
            )

            if existing:
                task = existing[0]

                if task["status"] != "error":
                    continue  # Skip active/completed duplicate

                # Reuse the existing failed task instead of inserting a
                # second row, which would violate the UNIQUE(url) index.
                tid = task["id"]
                task_update(
                    tid,
                    status="queued",
                    artist=None,
                    title=None,
                    error=None,
                    error_type=None,
                    retry_count=0,
                )
            else:
                cur = db_exec(
                    "INSERT INTO tasks(url, status) VALUES(?, 'queued')",
                    (link,),
                )
                tid = cur.lastrowid

            TASK_QUEUE.put((tid, link))
            PROCESSING_URLS.add(link)
            ids.append(tid)

    return {"added": ids}


@app.get("/api/tasks")
def tasks(authenticated: bool = Depends(verify_token)):
    return db_query("SELECT * FROM tasks ORDER BY id DESC LIMIT 50")


TRASH_DIR = PROJECT.parent / "trash"


class DeleteRequest(BaseModel):
    path: str


def library_track(rel_path: str) -> Path:
    """Resolve a library-relative path, refusing anything that escapes the library.

    The API listens on the LAN, so a caller must never be able to reach a file
    outside the music folder by sending ``../`` or an absolute path.
    """
    candidate = (LIBRARY / rel_path).resolve()
    root = LIBRARY.resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Path is outside the library")
    if not candidate.is_file() or candidate.suffix.lower() != ".m4a":
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

        root = LIBRARY.resolve()
        rows = []
        for f in sorted(root.rglob("*.m4a")):
            try:
                tags = MP4(f).tags or {}
            except Exception:
                continue
            artist = " • ".join(tags.get("\xa9ART") or [])
            title = (tags.get("\xa9nam") or [f.stem])[0]
            album = (tags.get("\xa9alb") or [""])[0]
            track = tags.get("trkn") or []
            rows.append({
                "path": str(f.relative_to(root)),
                "artist": artist,
                "title": title,
                "album": album,
                "track": track[0][0] if track else None,
                "albumartist": (tags.get("aART") or [""])[0],
                "haystack": f"{artist} {title} {album}".lower(),
            })

        _LIBRARY_INDEX.update({"at": time.time(), "rows": rows})
        return list(rows)


def invalidate_library_index() -> None:
    _LIBRARY_INDEX["at"] = 0.0


@app.get("/api/library")
def library(q: str = "", limit: int = 200, authenticated: bool = Depends(verify_token)):
    """List library tracks, optionally filtered by a substring of artist/title/album."""
    needle = q.strip().lower()
    out = []
    for row in library_index():
        if needle and needle not in row["haystack"]:
            continue
        out.append({k: row[k] for k in ("path", "artist", "title", "album", "track")})
        if len(out) >= limit:
            break
    return out


@app.delete("/api/library")
def delete_track(req: DeleteRequest, authenticated: bool = Depends(verify_token)):
    """Remove a track from the library.

    The file is moved to a trash folder rather than unlinked, so a mistaken tap
    on a phone stays recoverable. Navidrome's watcher notices the file is gone
    and drops it from the library on its own.
    """
    target = library_track(req.path)
    destination = TRASH_DIR / req.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}-{int(time.time())}.m4a")
    shutil.move(str(target), str(destination))

    # Leave no empty artist/album folders behind.
    for parent in target.parents:
        if parent == LIBRARY.resolve():
            break
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        else:
            break

    invalidate_library_index()
    logger.info("Deleted %s -> %s", req.path, destination)
    return {"deleted": req.path, "trash": str(destination)}


def library_counts() -> tuple[int, int]:
    """Track and album totals for the panel header, off the shared index."""
    try:
        rows = library_index()
    except Exception:
        return 0, 0
    albums = {(r["album"], r["albumartist"]) for r in rows if r["album"]}
    return len(rows), len(albums)


@app.get("/health")
def health():
    """Health endpoint (Problem #21)."""
    try:
        # Check database connectivity
        db_exec("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {str(e)[:100]}"

    # Check library path
    library_status = "ok" if LIBRARY.exists() else f"not found: {LIBRARY}"

    # Queue stats
    queue_size = TASK_QUEUE.qsize()

    healthy = db_status == "ok" and library_status == "ok"
    tracks, albums = library_counts() if library_status == "ok" else (0, 0)

    payload = {
        "status": "healthy" if healthy else "unhealthy",
        "database": db_status,
        "library": library_status,
        "library_path": str(LIBRARY),
        "workers": MAX_WORKERS,
        "queue_size": queue_size,
        "max_queue_size": MAX_QUEUE_SIZE,
        "tracks": tracks,
        "albums": albums,
    }

    if not healthy:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload,
        )

    return payload


@app.get("/", response_class=HTMLResponse)
def index():
    return (PROJECT.parent / "web" / "index.html").read_text()
