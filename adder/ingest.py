"""Turning a source into a library track: naming, downloading, tagging, filing.

The pipeline is split in two on purpose. ``download_to_temp`` is the part that
knows about YouTube; ``ingest_temp_file`` is the part that knows about the
library. Importing a file from disk reuses the second half untouched, which is
what keeps "nothing enters the library unchecked" true for every source rather
than only for links.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from mutagen.mp4 import MP4, MP4Cover

from . import config, db, enrich, library, runtime

logger = logging.getLogger(__name__)


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
    if not config.PRESERVE_FEAT_ARTISTS:
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
                # runtime.shutdown_event itself. Treat that subprocess termination
                # as an intentional shutdown, not as a task failure.
                if runtime.shutdown_event.is_set():
                    raise runtime.ShutdownRequested()

                break

            if runtime.shutdown_event.is_set():
                logger.info(
                    "Stopping yt-dlp subprocess due to shutdown",
                    extra={"task_id": "system"},
                )
                terminate_process()
                drain_pipes()
                raise runtime.ShutdownRequested()

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
    # nice goes in the command rather than on the systemd unit: yt-dlp and the
    # ffmpeg it spawns are children of this unit and inherit its priority, so
    # Nice= there would move the service as a whole and change nothing between
    # a download and a stream inside it. preexec_fn would work too, but it is
    # documented as unsafe in a threaded process, and this one is threaded.
    command = ["nice", "-n", "10", sys.executable, "-m", "yt_dlp"]
    if config.COOKIES_FROM_BROWSER:
        command += ["--cookies-from-browser", config.COOKIES_FROM_BROWSER]
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
        str(runtime.TMP_DIR / f"{vid}.%(ext)s"),
        url,
    ]

    p = run_yt_dlp(command, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-300:])
    target = runtime.TMP_DIR / f"{vid}.m4a"
    if not target.exists():
        found = list(runtime.TMP_DIR.glob(f"{vid}.*"))
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

    if not config.LIBRARY.exists():
        return None

    for candidate in config.LIBRARY.rglob("*.m4a"):
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


def check_disk_space() -> tuple[bool, int]:
    """Check if there's enough free disk space (Problem #30).

    Returns:
        (has_space, free_mb)
    """
    try:
        import shutil

        stat = shutil.disk_usage(runtime.TMP_DIR)
        free_mb = stat.free // (1024 * 1024)
        return free_mb >= config.MIN_FREE_SPACE_MB, free_mb
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
    if not runtime.TMP_DIR.exists():
        return

    current_time = time.time()
    cleaned = 0

    for f in runtime.TMP_DIR.glob("*"):
        try:
            # Don't delete files that are currently being processed
            mtime = f.stat().st_mtime
            age_seconds = current_time - mtime

            if age_seconds > runtime.TMP_TTL_SECONDS:
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


@dataclass(frozen=True)
class TrackNames:
    """What a track should be called, once the source has been interrogated.

    ``fs_*`` are sanitised for the filesystem, ``meta_*`` keep the punctuation
    and version markers that belong in the tags.
    """

    fs_artist: str
    fs_title: str
    full_artist: str
    meta_title: str


@dataclass(frozen=True)
class Downloaded:
    temp_path: Path
    names: TrackNames
    thumbnail: str | None


def stage_into_temp(source: Path, key: str) -> Path:
    """Move an already-obtained audio file into the processing directory.

    Nothing reaches the library from here; this only gets the bytes to the one
    place where tagging and duplicate checks are allowed to happen.
    """
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = runtime.TMP_DIR / f"{key}_processing.m4a"
    shutil.move(str(source), str(temp_path))
    return temp_path


def download_to_temp(tid: int, url: str) -> Downloaded:
    """Fetch a link and leave a validated file in the temp directory.

    The head of the pipeline: everything that is specific to YouTube lives
    here, so a different source can supply its own head and still hand the
    result to ``ingest_temp_file``.
    """
    has_space, free_mb = check_disk_space()
    if not has_space:
        raise RuntimeError(
            f"Insufficient disk space: {free_mb}MB free, {config.MIN_FREE_SPACE_MB}MB required"
        )

    db.task_update(tid, status="downloading")
    meta = yt_meta(url)
    fs_artist, fs_title, full_artist, meta_title = split_artist_title(meta)

    downloaded = yt_download(url, meta["id"])
    if not downloaded.exists():
        raise RuntimeError("Downloaded file not found")

    is_valid, error_msg = validate_m4a_integrity(downloaded)
    if not is_valid:
        raise RuntimeError(error_msg)

    return Downloaded(
        temp_path=stage_into_temp(downloaded, meta["id"]),
        names=TrackNames(fs_artist, fs_title, full_artist, meta_title),
        thumbnail=meta.get("thumbnail"),
    )


def ingest_temp_file(tid: int, temp_path: Path, names: TrackNames, thumbnail: str | None) -> str:
    """Tag a staged file and move it into the library, or discard it as a duplicate.

    The tail of the pipeline, and the only path by which anything reaches the
    library. Returns "stored" or "duplicate"; raises on anything that should
    fail the task.
    """
    db.task_update(tid, status="tagging", artist=names.full_artist, title=names.meta_title)

    # YouTube gives us a title and an uploader; Deezer gives us the album,
    # the track number and the real list of artists. Without this the track
    # lands in a nameless bucket with no position, which is what made every
    # album in the library read "Singles" in the first place.
    info, from_deezer = enrich.describe(names.full_artist, names.meta_title)
    logger.info(
        "Metadata for %r by %r: album=%r track=%s source=%s",
        names.meta_title,
        names.full_artist,
        info.album,
        info.track_number,
        "deezer" if from_deezer else "fallback",
    )

    cover, fmt = None, None
    if info.cover_url:
        cover, fmt = fetch_cover_url(info.cover_url)
    if not cover:
        cover, fmt = fetch_cover(names.fs_artist, names.fs_title, thumbnail)

    # Folder layout is left alone on purpose: Navidrome groups albums by
    # tags, not by directory, so moving files would buy nothing.
    target_dir = config.LIBRARY / names.fs_artist / "Singles"
    target_dir.mkdir(parents=True, exist_ok=True)
    base_target = target_dir / f"{names.fs_title}.m4a"

    audio = MP4(temp_path)
    audio["\xa9nam"] = [names.meta_title]  # Full title with version info
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

    audio_verify = MP4(temp_path)
    if not audio_verify.get("\xa9nam"):
        raise RuntimeError("Metadata write failed verification")

    is_valid, error_msg = validate_m4a_integrity(temp_path)
    if not is_valid:
        raise RuntimeError(f"Final validation failed: {error_msg}")

    # Content-based duplicate detection must happen while holding the same
    # lock as the final move. This prevents concurrent workers from both
    # accepting identical audio.
    with runtime.FILE_LOCK:
        duplicate = find_duplicate_library_file(temp_path)

        if duplicate is not None:
            logger.info(
                f"Duplicate content detected; keeping existing file "
                f"{duplicate} and discarding temporary file {temp_path}",
                extra={"task_id": tid},
            )
            temp_path.unlink()
            return "duplicate"

        # Same filename + different content is allowed.
        # Preserve the existing collision-safe naming behavior.
        final_target = unique_path(base_target)
        shutil.move(str(temp_path), str(final_target))

    library.invalidate_library_index()  # a new track must show up in search now
    return "stored"
