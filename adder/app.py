"""YouTube -> Navidrome: веб-интерфейс + фоновые воркеры."""
import logging
import os
import signal
import json
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from mutagen.mp4 import MP4, MP4Cover
from pydantic import BaseModel

# Import unified configuration
from .config import (
    LIBRARY, PORT, HOST, MAX_WORKERS, DELAY_BETWEEN_TRACKS,
    MAX_LINKS_PER_REQUEST, MAX_QUEUE_SIZE, YT_SEARCH_COUNT, YT_MATCH_MIN_SCORE,
    PRESERVE_FEAT_ARTISTS, API_TOKEN, MAX_RETRIES, RETRY_BACKOFF_BASE, SHUTDOWN_TIMEOUT,
    MIN_FREE_SPACE_MB, TMP_TTL_HOURS
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
    format='[%(asctime)s] [task=%(task_id)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Apply the tolerant formatter to the root handlers so third-party
# libraries (httpx, uvicorn, etc.) cannot trigger KeyError.
for handler in logging.getLogger().handlers:
    handler.setFormatter(TaskIdFormatter(
        '[%(asctime)s] [task=%(task_id)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

logger = logging.getLogger(__name__)

# Problem #19: API Token authentication
security = HTTPBearer(auto_error=False)

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> bool:
    """Verify API token if configured (Problem #19)."""
    if not API_TOKEN:
        return True  # No token configured, allow all
    
    if credentials is None or credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True

# Problem #22: Graceful shutdown state
shutdown_event = threading.Event()
active_workers = []

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
    db_exec(f"UPDATE tasks SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
            (*fields.values(), tid))

def recover_queued_tasks():
    """Recovery tasks after restart (Problem #6)."""
    # Find all tasks that were not completed
    queued = db_query("SELECT id, url, status FROM tasks WHERE status IN ('queued', 'downloading', 'tagging')")
    recovered = 0
    for task in queued:
        # Reset interrupted tasks to queued
        if task['status'] in ('downloading', 'tagging'):
            task_update(task['id'], status='queued')
        # Add to queue (avoiding duplicates)
        with FILE_LOCK:
            if task['url'] not in PROCESSING_URLS:
                TASK_QUEUE.put((task['id'], task['url']))
                PROCESSING_URLS.add(task['url'])
                recovered += 1
    if recovered > 0:
        logger.info(f"Recovered {recovered} tasks from previous session", extra={'task_id': 'system'})

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
    
    return sanitize_filename(fs_artist), sanitize_filename(clean_title(title, for_filename=True)), full_artist, clean_title(title, for_filename=False)

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
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
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
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
                    break

                if not chunk:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
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
                        try:
                            selector.unregister(stream)
                        except Exception:
                            pass
                        break

                    if not chunk:
                        try:
                            selector.unregister(stream)
                        except Exception:
                            pass
                        break

                    if key.data == "stdout":
                        stdout_chunks.append(chunk)
                    else:
                        stderr_chunks.append(chunk)

            if process.poll() is not None:
                drain_pipes()
                break

            if shutdown_event.is_set():
                logger.info(
                    "Stopping yt-dlp subprocess due to shutdown",
                    extra={"task_id": "system"},
                )
                terminate_process()
                drain_pipes()
                raise RuntimeError("Shutdown requested while yt-dlp was running")

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


def yt_meta(url: str) -> dict:
    p = run_yt_dlp(
        [sys.executable, "-m", "yt_dlp", "-J", "--no-playlist", url],
        timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    return json.loads(p.stdout)

def yt_search_candidates(query: str, count: int = None) -> list[dict]:
    """Search YouTube for multiple candidates and return scored results (Problem #11)."""
    if count is None:
        count = YT_SEARCH_COUNT
    
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist",
        "--no-download",
        "--no-warnings",
        "-j",
        f"ytsearch{count}:{query}"
    ]
    
    try:
        result = run_yt_dlp(cmd, timeout=60)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        
        candidates = []
        for line in result.stdout.strip().splitlines():
            try:
                data = json.loads(line)
                candidates.append(data)
            except json.JSONDecodeError:
                continue
        return candidates
    except Exception:
        return []

def score_candidate(candidate: dict, query_artist: str, query_title: str, query_duration: float = None) -> float:
    """Score a YouTube candidate based on similarity to query (Problem #11).
    
    Returns:
        Score between 0.0 and 1.0
    """
    candidate_title = (candidate.get('title') or '').lower()
    candidate_uploader = (candidate.get('uploader') or candidate.get('channel') or '').lower()
    candidate_duration = candidate.get('duration', 0)
    
    query_artist_lower = query_artist.lower()
    query_title_lower = query_title.lower()
    
    # Title similarity (45% weight)
    title_words = set(query_title_lower.split())
    candidate_title_words = set(candidate_title.split())
    title_overlap = len(title_words & candidate_title_words) / max(len(title_words), 1)
    title_score = title_overlap * 0.45
    
    # Artist similarity (35% weight)
    artist_words = set(query_artist_lower.split())
    candidate_artist_words = set(candidate_uploader.split())
    artist_overlap = len(artist_words & candidate_artist_words) / max(len(artist_words), 1)
    artist_score = artist_overlap * 0.35
    
    # Duration similarity (20% weight)
    duration_score = 0.0
    if query_duration and candidate_duration:
        duration_diff = abs(candidate_duration - query_duration) / max(query_duration, 1)
        if duration_diff < 0.1:
            duration_score = 0.20
        elif duration_diff < 0.2:
            duration_score = 0.15
        elif duration_diff < 0.3:
            duration_score = 0.10
        else:
            duration_score = 0.05
    else:
        duration_score = 0.10  # Neutral if no duration info
    
    total_score = title_score + artist_score + duration_score
    
    # Penalty for obvious non-matches
    penalty_keywords = ['lyrics', 'karaoke', 'slowed', 'sped', 'remix', 'live', 'cover']
    for kw in penalty_keywords:
        if kw in candidate_title and kw not in query_title_lower:
            total_score *= 0.8
    
    return min(total_score, 1.0)

def find_best_youtube_match(artist: str, title: str, duration: float = None) -> tuple[str | None, float]:
    """Find the best YouTube match for a track (Problem #11).
    
    Returns:
        (best_url, confidence_score) or (None, 0.0) if no good match found
    """
    query = f"{artist} - {title}"
    candidates = yt_search_candidates(query, YT_SEARCH_COUNT)
    
    if not candidates:
        return None, 0.0
    
    best_candidate = None
    best_score = 0.0
    
    for candidate in candidates:
        score = score_candidate(candidate, artist, title, duration)
        if score > best_score:
            best_score = score
            best_candidate = candidate
    
    if best_score >= YT_MATCH_MIN_SCORE and best_candidate:
        url = best_candidate.get('url') or best_candidate.get('webpage_url')
        return url, best_score
    
    return None, best_score

def yt_download(url: str, vid: str) -> Path:
    p = run_yt_dlp(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "-x",
            "--audio-format",
            "m4a",
            "--audio-quality",
            "0",
            "--no-playlist",
            "-o",
            str(TMP_DIR / f"{vid}.%(ext)s"),
            url,
        ],
        timeout=600,
    )
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
        q = f"{artist} {title}".replace(" ", "+")
        r = requests.get(f"https://itunes.apple.com/search?term={q}&limit=1&entity=song", timeout=10)
        if r.ok and r.json().get("resultCount", 0) > 0:
            art = r.json()["results"][0].get("artworkUrl100", "").replace("100x100bb", "3000x3000bb")
            img = requests.get(art, timeout=15)
            if img.ok:
                return img.content, "jpg"
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
        if not audio.info or not hasattr(audio.info, 'length') or audio.info.length <= 0:
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
                logger.info(f"Cleaned up old temp file: {f.name}", extra={'task_id': 'system'})
        except OSError as e:
            logger.warning(
                f"Could not clean temp file {f.name}: {e}",
                extra={"task_id": "system"},
            )
    
    if cleaned > 0:
        logger.info(f"Cleaned {cleaned} old temp files", extra={'task_id': 'system'})

def process(tid: int, url: str):
    tmp_file = None
    temp_path = None
    retry_count = 0
    last_error_type = None
    last_error_msg = ""
    
    while retry_count < MAX_RETRIES:
        try:
            # Problem #30: Check disk space before download
            has_space, free_mb = check_disk_space()
            if not has_space:
                raise RuntimeError(f"Insufficient disk space: {free_mb}MB free, {MIN_FREE_SPACE_MB}MB required")
            
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
            cover, fmt = fetch_cover(fs_artist, fs_title, meta.get("thumbnail"))
            
            # Determine final destination
            target_dir = LIBRARY / fs_artist / "Singles"
            target_dir.mkdir(parents=True, exist_ok=True)
            base_target = target_dir / f"{fs_title}.m4a"
            
            # Problem #8: Process metadata on temp file BEFORE moving to library
            audio = MP4(tmp_file)
            audio["\xa9nam"] = meta_title  # Full title with version info
            audio["\xa9ART"] = full_artist  # Full artist metadata
            audio["aART"] = full_artist
            audio["\xa9alb"] = "Singles"
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
            
            # Problem #7 & #8: Atomic move to library ONLY after successful processing
            with FILE_LOCK:
                final_target = unique_path(base_target)
                shutil.move(str(tmp_file), str(final_target))
            
            tmp_file = None  # Successfully moved, don't cleanup in finally
            
            # Problem #24: Clear error fields on success
            task_update(tid, status="done", error="", error_type="")
            return  # Success, exit retry loop
            
        except Exception as e:
            error_str = str(e)[:300]
            last_error_msg = error_str
            
            # Problem #24: Classify error type
            error_lower = error_str.lower()
            if "invalid url" in error_lower or "url" in error_lower:
                last_error_type = "invalid_url"
            elif "not found" in error_lower or "unavailable" in error_lower:
                last_error_type = "youtube_not_found"
            elif "download" in error_lower:
                last_error_type = "download_error"
            elif "metadata" in error_lower:
                last_error_type = "metadata_error"
            elif "artwork" in error_lower or "cover" in error_lower:
                last_error_type = "artwork_error"
            elif "disk" in error_lower or "space" in error_lower:
                last_error_type = "filesystem_error"
            elif "database" in error_lower or "sqlite" in error_lower:
                last_error_type = "database_error"
            elif "network" in error_lower or "timeout" in error_lower or "http" in error_lower:
                last_error_type = "network_error"
            else:
                last_error_type = "unknown_error"
            
            # Problem #23: Retry logic for transient errors
            retryable_errors = {"network_error", "download_error", "artwork_error"}
            
            if last_error_type in retryable_errors and retry_count < MAX_RETRIES - 1:
                retry_count += 1
                backoff_time = RETRY_BACKOFF_BASE ** retry_count
                logger.warning(
                    f"Retry {retry_count}/{MAX_RETRIES} after {backoff_time}s: {error_str}",
                    extra={'task_id': tid},
                )

                # Allow SIGTERM/shutdown to interrupt retry backoff immediately.
                if shutdown_event.wait(backoff_time):
                    logger.info(
                        "Shutdown requested during retry backoff",
                        extra={'task_id': tid},
                    )

                    # The normal cleanup below the retry loop is skipped by
                    # this early return, so release the processing lock here.
                    with FILE_LOCK:
                        PROCESSING_URLS.discard(url)

                    return

                continue
            
            # Not retryable or max retries reached
            task_update(tid, status="error", error=error_str, 
                       error_type=last_error_type, retry_count=retry_count)
            
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
    db_init()

    # Recover tasks left unfinished by a previous process.
    recover_queued_tasks()

    # Remove stale temporary files from previous runs.
    cleanup_old_temp_files()

    # Start background workers.
    # Each worker consumes tasks from the shared queue and processes them.
    shutdown_event.clear()
    active_workers.clear()

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

class AddRequest(BaseModel):
    links: list[str]

@app.post("/api/add")
def add(req: AddRequest, authenticated: bool = Depends(verify_token)):
    """Add YouTube links to queue (Problem #19: API auth)."""
    # Problem #9: Check request limits
    if len(req.links) > MAX_LINKS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many links. Maximum {MAX_LINKS_PER_REQUEST} per request."
        )
    
    # Problem #9: Check queue size limit
    current_queue_size = TASK_QUEUE.qsize()
    if current_queue_size + len(req.links) > MAX_QUEUE_SIZE:
        raise HTTPException(
            status_code=429,
            detail=f"Queue full. Current: {current_queue_size}, Max: {MAX_QUEUE_SIZE}"
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
    if LIBRARY.exists():
        library_status = "ok"
    else:
        library_status = f"not found: {LIBRARY}"
    
    # Queue stats
    queue_size = TASK_QUEUE.qsize()
    
    return {
        "status": "healthy" if db_status == "ok" and library_status == "ok" else "unhealthy",
        "database": db_status,
        "library": library_status,
        "library_path": str(LIBRARY),
        "workers": MAX_WORKERS,
        "queue_size": queue_size,
        "max_queue_size": MAX_QUEUE_SIZE
    }

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Music Adder</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;max-width:760px;margin:0 auto;padding:16px}
textarea{width:100%;height:120px;background:#222;color:#eee;border:1px solid #444;border-radius:8px;padding:8px}
button{background:#2f7df6;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;margin-top:8px}
table{width:100%;border-collapse:collapse;margin-top:16px;font-size:14px}
td,th{padding:6px 4px;border-bottom:1px solid #333;text-align:left;vertical-align:top}
.done{color:#4caf50}.error{color:#f44336}.queued,.downloading,.tagging{color:#ffb300}
</style></head><body>
<h2>YouTube → Navidrome</h2>
<div id=authbox style="margin-bottom:12px">
  <input id=token type=password placeholder="API Token" autocomplete="off"
         style="width:70%;box-sizing:border-box;background:#222;color:#eee;border:1px solid #444;border-radius:8px;padding:9px">
  <button onclick=saveToken() style="margin-left:4px">Сохранить</button>
  <button onclick=clearToken() style="margin-left:4px;background:#555">Очистить</button>
</div>
<textarea id=links placeholder="https://www.youtube.com/watch?v=...&#10;https://youtu.be/..."></textarea>
<button onclick=add()>Добавить</button>
<table><thead><tr><th>Статус</th><th>Трек</th><th>URL</th></tr></thead><tbody id=tb></tbody></table>
<script>
const TOKEN_KEY = 'localSpotifyApiToken';

function getToken(){
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function saveToken(){
  const value = document.getElementById('token').value.trim();
  if(value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
  poll();
}

function clearToken(){
  sessionStorage.removeItem(TOKEN_KEY);
  document.getElementById('token').value = '';
  poll();
}

document.getElementById('token').value = getToken();

async function add(){
  const links=document.getElementById('links').value.split('\\n').map(s=>s.trim()).filter(Boolean);
  if(!links.length)return;
  const token = sessionStorage.getItem('localSpotifyApiToken') || '';
  const headers = {'Content-Type':'application/json'};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  await fetch('/api/add',{method:'POST',headers,body:JSON.stringify({links})});
  document.getElementById('links').value='';poll();
}
async function poll(){
  const token = sessionStorage.getItem('localSpotifyApiToken') || '';
  const headers = {};
  if (token) headers['Authorization'] = 'Bearer ' + token;

  try {
    const r = await fetch('/api/tasks', {headers});

    if (!r.ok) {
      return;
    }

    const tasks = await r.json();
    const tbody = document.getElementById('tb');

    tbody.replaceChildren();

    for (const task of tasks) {
      const row = document.createElement('tr');

      const statusCell = document.createElement('td');
      statusCell.className = task.status || '';
      statusCell.textContent = task.status || '';

      const trackCell = document.createElement('td');

      if (task.artist) {
        const artist = document.createElement('span');
        artist.textContent = task.artist + ' — ';
        trackCell.appendChild(artist);
      }

      if (task.title) {
        const title = document.createElement('span');
        title.textContent = task.title;
        trackCell.appendChild(title);
      }

      if (task.error) {
        const error = document.createElement('small');
        error.textContent = task.error;
        error.style.display = 'block';
        trackCell.appendChild(error);
      }

      const urlCell = document.createElement('td');
      const url = document.createElement('small');
      url.textContent = task.url || '';
      urlCell.appendChild(url);

      row.appendChild(statusCell);
      row.appendChild(trackCell);
      row.appendChild(urlCell);

      tbody.appendChild(row);
    }
  } catch (error) {
    console.error('Failed to fetch tasks:', error);
  }
}
setInterval(poll,2000);poll();
</script></body></html>"""
