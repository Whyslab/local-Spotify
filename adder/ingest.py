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
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import mutagen
import requests
from mutagen.mp4 import MP4, MP4Cover

from . import config, db, enrich, library, loudness, runtime

logger = logging.getLogger(__name__)


# Linux allows 255 bytes per path component. Leave room for " (12).m4a".
MAX_NAME_BYTES = 200


def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    name = re.sub(r"[\[\]'\"]", "", str(name))
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    # Управляющие символы из тегов и ведущие точки: «..» уводил бы из папки,
    # «.m4a» становился скрытым файлом без имени.
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".").strip()
    # Длинное имя из эмодзи или кириллицы не влезало в 255 байт и роняло
    # задачу с «File name too long». Режем по границе символа.
    encoded = name.encode("utf-8")
    if len(encoded) > MAX_NAME_BYTES:
        name = encoded[:MAX_NAME_BYTES].decode("utf-8", errors="ignore").strip()
    return name or "Unknown"


JUNK = [
    r"official\s+(music\s+)?(video|audio|lyric\s+video|clip)",
    r"official\s+(video|audio)",
    r"(lyric(s)?\s+video|visuali[sz]er|music\s+video)",
    r"премьера(\s+(трека|клипа))?",
    r"текст\s+песни",
    # «(текст)», «[lyrics]», «(audio)», «(клип)» — пометки ролика, а не названия.
    r"[(\[]\s*(текст|lyrics|audio|клип|clip)\s*[)\]]",
    # «Song + lyrics», «Песня - текст» в конце: ролик с текстом на экране.
    # Только через «+» или тире — «Мой текст» может быть названием песни.
    r"\s*(\+|\s[-–—|])\s*(текст|lyrics)\s*$",
]

# Version keywords that should be preserved in metadata (Problem #13)
_VERSION = r"(live|remix|acoustic|radio\s+edit|remastered|deluxe|explicit|clean)"
# Only where a version marker actually sits: in brackets, or after a dash at
# the end ("Song - Remastered 2011"). A bare word anywhere used to be removed
# too, so "Live Forever" was filed as "Forever" and "Clean Up" as "Up".
VERSION_KEYWORDS = [
    rf"\({_VERSION}\b[^)]*\)",
    rf"\[{_VERSION}\b[^\]]*\]",
    rf"\s[-–—]\s*{_VERSION}\b[^()\[\]]*$",
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


def channel_artist(uploader: str) -> str:
    """The artist behind a channel name, as far as the name itself tells.

    YouTube's auto-generated channels are "<Artist> - Topic" and label-run
    ones "<Artist>VEVO"; used verbatim, those became the artist folder and
    the Deezer query, which then found nothing. Camel case is left alone:
    "TaylorSwift" could be split, "McFly" could not.
    """
    name = re.sub(r"\s+-\s+topic$", "", uploader.strip(), flags=re.IGNORECASE)
    name = re.sub(r"\s*vevo$", "", name, flags=re.IGNORECASE)
    return name.strip()


def split_artist_title(meta: dict):
    artist = meta.get("artist") or meta.get("creator") or ""
    title = meta.get("track") or meta.get("title") or "Unknown"
    if not artist and " - " in title:
        artist, title = title.split(" - ", 1)
    elif artist and not meta.get("track"):
        # YouTube Music fills "artist" but not always "track"; the video title
        # then repeats the artist, which used to end up inside the title tag.
        prefix = re.match(rf"\s*{re.escape(artist)}\s+[-–—]\s+", title, flags=re.IGNORECASE)
        if prefix:
            title = title[prefix.end() :]
    if not artist:
        artist = channel_artist(meta.get("uploader") or "") or "Unknown Artist"

    # Problem #12: Preserve full artist metadata
    full_artist = artist.strip()

    # For filesystem, use primary artist only (safe naming)
    if not config.PRESERVE_FEAT_ARTISTS:
        # Whole words only: splitting on the bare substrings " feat" and " ft"
        # also cut names such as "Joe Feather" or "Lefty Ftw" short.
        fs_artist = re.split(
            r",|\s+(?:feat\.?|ft\.?|featuring)(?=\s)", artist, maxsplit=1, flags=re.IGNORECASE
        )[0]
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
        # Shorts, live replays and embeds are ordinary videos under another
        # path; they were refused with "must use /watch" before.
        other_form = re.fullmatch(r"/(?:shorts|live|embed|v)/([^/]+)/?", parsed.path)
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif other_form:
            video_id = other_form.group(1)
        else:
            raise ValueError("YouTube URL must use /watch?v=VIDEO_ID")

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


def ytdlp_env() -> dict[str, str]:
    """The environment for yt-dlp: its own and Deno's cache in a writable place."""
    runtime.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "XDG_CACHE_HOME": str(runtime.CACHE_DIR)}


def run_yt_dlp(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run yt-dlp with timeout and shutdown-aware subprocess handling.

    The subprocess is placed in its own process group so that yt-dlp and
    children such as ffmpeg can be terminated together during shutdown.

    stdout and stderr are drained continuously in non-blocking mode to
    prevent pipe-buffer deadlocks when yt-dlp produces a large amount
    of output.
    """
    import selectors

    # YouTube rate-limited an earlier call: wait it out here, before the
    # timeout starts counting, and give way at once to a shutdown.
    left = runtime.youtube_pause_left()
    if left and runtime.shutdown_event.wait(left):
        raise runtime.ShutdownRequested()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
        env=ytdlp_env(),
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
                    chunk = os.read(key.fd, 65536)
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
                        chunk = os.read(key.fd, 65536)
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


def check_dependencies() -> dict[str, str]:
    """Report the external programs yt-dlp relies on that are missing from PATH.

    ffmpeg and ffprobe are required: yt-dlp needs them to extract the audio, so
    without them every task fails in post-processing. Deno is the JavaScript
    runtime yt-dlp uses by default to solve YouTube's player challenges;
    without it some videos still download, others lose formats or fail.
    """
    return {
        "ffmpeg": "ok" if shutil.which("ffmpeg") and shutil.which("ffprobe") else "missing",
        "js_runtime": "ok" if shutil.which("deno") else "missing",
    }


def log_missing_dependencies() -> None:
    deps = check_dependencies()
    if deps["ffmpeg"] != "ok":
        logger.error(
            "ffmpeg/ffprobe not found on PATH: every download will fail. "
            "Install ffmpeg (e.g. sudo apt install ffmpeg) and restart the service.",
            extra={"task_id": "system"},
        )
    if deps["js_runtime"] != "ok":
        logger.warning(
            "Deno not found on PATH: yt-dlp needs it to solve YouTube's JavaScript "
            "challenges, so some downloads may fail. See README, Quick start.",
            extra={"task_id": "system"},
        )


def ytdlp_error(stderr: str) -> str:
    """The reason yt-dlp gave for failing, not just the tail of its output.

    yt-dlp states the cause on an ``ERROR:`` line that is often longer than
    the 300 characters kept for a task, so taking the tail of stderr kept the
    trailing FAQ links and cut off the cause itself.
    """
    errors = [line.strip() for line in stderr.splitlines() if line.strip().startswith("ERROR:")]
    if errors:
        return errors[-1]
    return stderr.strip()[-300:] or "yt-dlp failed without an error message"


def yt_meta(url: str) -> dict:
    p = run_yt_dlp(
        [*ytdlp_base(), "-J", "--no-playlist", url],
        timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(ytdlp_error(p.stderr))
    return json.loads(p.stdout)


def check_downloadable(meta: dict) -> None:
    """Refuse live streams and over-long videos before downloading anything.

    A stream never ends, so it ran into the 10-minute download timeout and was
    then retried; a many-hour mix did the same. Both held a worker for half an
    hour to produce nothing useful as a track.
    """
    if meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError("Unsupported video: live streams cannot be added as tracks")
    limit = config.MAX_DURATION_MINUTES
    duration = meta.get("duration") or 0
    if limit and duration > limit * 60:
        raise RuntimeError(
            f"Unsupported video: {duration // 60} min is longer than MAX_DURATION_MINUTES={limit}"
        )


# YouTube's best audio is usually Opus. Converting that to AAC is a second
# lossy pass; most videos also carry a native AAC stream, which "-x" then only
# remuxes into .m4a. Conversion remains the fallback when there is none.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"


YTDLP_LEFTOVERS = {".part", ".ytdl", ".temp", ".tmp"}


def yt_download(url: str, vid: str) -> Path:
    command = [
        *ytdlp_base(),
        "-f",
        AUDIO_FORMAT,
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
        raise RuntimeError(ytdlp_error(p.stderr))
    target = runtime.TMP_DIR / f"{vid}.m4a"
    if not target.exists():
        # yt-dlp's own leftovers are never the result: a .part is an unfinished
        # download, .ytdl its resume state, .temp a half-written conversion.
        found = sorted(
            p
            for p in runtime.TMP_DIR.glob(f"{vid}.*")
            if p.is_file() and p.suffix not in YTDLP_LEFTOVERS
        )
        if not found:
            raise RuntimeError("yt-dlp finished but produced no audio file")
        target = found[0]
    return target


def image_format(data: bytes | None) -> str | None:
    """ "jpg" or "png" when the bytes really are that image, else None.

    A cover source can answer 200 with an HTML error page, and YouTube
    thumbnails are often WebP; either used to be embedded labelled as JPEG.
    """
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def _download_image(url: str):
    try:
        img = requests.get(url, timeout=15)
    except Exception:
        return None, None
    fmt = image_format(img.content) if img.ok else None
    return (img.content, fmt) if fmt else (None, None)


def _itunes_match(results: list[dict], artist: str, title: str) -> dict | None:
    """The result that is this song, not merely the top hit for the words."""
    want_title = enrich.normalize(title)
    want_artist = enrich.normalize((enrich.split_artists(artist) or [artist])[0])
    for result in results:
        if enrich.normalize(result.get("trackName") or "") != want_title:
            continue
        listed = result.get("artistName") or ""
        names = {enrich.normalize(n) for n in [listed, *enrich.split_artists(listed)]}
        if want_artist and want_artist not in names:
            continue
        return result
    return None


def get_hd_cover(artist: str, title: str):
    """Возвращает (bytes, fmt) или (None, None)."""
    try:
        params: dict[str, str | int] = {"term": f"{artist} {title}", "limit": 5, "entity": "song"}
        r = requests.get("https://itunes.apple.com/search", params=params, timeout=10)
        if not r.ok:
            return None, None
        # The top hit alone gave obscure tracks someone else's artwork.
        match = _itunes_match(r.json().get("results") or [], artist, title)
        art = (match or {}).get("artworkUrl100", "")
        if art:
            return _download_image(art.replace("100x100bb", "3000x3000bb"))
    except Exception:
        pass
    return None, None


def fetch_cover_url(url: str):
    """Download album art from a known-good URL (Deezer gives us one per album)."""
    return _download_image(url)


def youtube_jpeg_thumbnail(url: str) -> str:
    """The JPEG rendition of a YouTube thumbnail URL that points at WebP.

    i.ytimg.com serves every thumbnail both ways: /vi_webp/ID/x.webp and
    /vi/ID/x.jpg. MP4 cover art can only be JPEG or PNG.
    """
    if "/vi_webp/" in url and url.endswith(".webp"):
        return url.replace("/vi_webp/", "/vi/")[: -len(".webp")] + ".jpg"
    return url


def fetch_cover(artist: str, title: str, thumb_url: str | None):
    data, fmt = get_hd_cover(artist, title)
    if data:
        return data, fmt
    if thumb_url:  # fallback: превью YouTube
        return _download_image(youtube_jpeg_thumbnail(thumb_url))
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

    for candidate in (
        p
        for suffix in library.AUDIO_SUFFIXES
        for p in config.LIBRARY.rglob(f"*{suffix}")
        # An artist folder can be named like a file -- see "nyan.mp3".
        if p.is_file()
    ):
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


# How far apart two recordings of one song may be and still count as the same.
SIMILAR_DURATION_SECONDS = 3.0


def audio_duration(path: Path) -> float | None:
    try:
        parsed = mutagen.File(path)
    except Exception:
        return None
    length = getattr(getattr(parsed, "info", None), "length", 0) or 0
    return float(length) or None


def find_similar_library_track(
    artists: list[str], title: str, duration: float | None
) -> str | None:
    """A library track that is probably the same song, or None.

    The content hash only catches byte-identical files; the same song from a
    second video (official clip, lyric video, a Topic upload) never matches it
    and used to land silently as "Song (1)". This compares what a listener
    would: the title and lead artist, folded by enrich.normalize, and the
    length within a few seconds when both are known. A version marker such as
    "(Live)" is part of the title, so a live take is not flagged.
    """
    want_title = enrich.normalize(title)
    want_artist = enrich.normalize(artists[0]) if artists else ""
    if not want_title:
        return None
    for row in library.library_index():
        if enrich.normalize(row.get("title") or "") != want_title:
            continue
        row_artists = [enrich.normalize(a) for a in enrich.split_artists(row.get("artist") or "")]
        row_artists.append(enrich.normalize(row.get("albumartist") or ""))
        if want_artist and want_artist not in row_artists:
            continue
        other = row.get("duration")
        if duration and other and abs(duration - other) > SIMILAR_DURATION_SECONDS:
            continue
        return row["path"]
    return None


def unique_path(base: Path) -> Path:
    p, n = base, 1
    while p.exists():
        p = base.with_name(f"{base.stem} ({n}){base.suffix}")
        n += 1
    return p


def check_disk_space() -> tuple[bool, int]:
    """Check if there's enough free disk space (Problem #30).

    Both ends of the move count: the temp directory the download lands in and
    the library it ends up in, which is often another disk. The smaller of
    the two is what decides.

    Returns:
        (has_space, free_mb)
    """
    free = []
    for path in (runtime.TMP_DIR, config.LIBRARY):
        # The library may not exist yet on a first run; its parent is the disk.
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            free.append(shutil.disk_usage(path).free // (1024 * 1024))
        except OSError as exc:
            logger.warning(
                "Could not check free space on %s: %s", path, exc, extra={"task_id": "system"}
            )
    if not free:
        return True, 0  # If we can't check, allow operation
    free_mb = min(free)
    return free_mb >= config.MIN_FREE_SPACE_MB, free_mb


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


# ffmpeg complains about plenty of things that do not make a file unplayable:
# junk in the tags, a ragged VBR frame, a stray byte before the first header.
# Requiring silence would reject legitimate mp3s, so the test is the exit code
# plus this list -- the messages that mean the audio itself is broken.
FATAL_DECODE_PATTERNS = (
    "invalid data found",
    "moov atom not found",
    "error while decoding",
    "truncated",
    "could not find codec parameters",
    "end of file",
)


def decodes_cleanly(filepath: Path, timeout: float = 120) -> tuple[bool, str, float | None]:
    """Read the whole stream through ffmpeg and report how far it got.

    Parsing the header only proves the file starts like audio. This decodes it
    to the end, and returns the number of seconds that actually came out --
    which is the only way to see the failure that matters. A download cut off
    halfway keeps a perfectly good header: an mp3 truncated to a third still
    claims its original length, ffmpeg still exits 0, and it still says
    nothing. It just stops early. The caller compares the two numbers.
    """
    try:
        result = subprocess.run(
            [
                "nice",
                "-n",
                "10",
                "ffmpeg",
                "-v",
                "error",
                "-stats",
                "-i",
                str(filepath),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out reading the file", None
    except FileNotFoundError:
        # No ffmpeg means no second opinion; the header check still ran.
        return True, "", None

    if result.returncode != 0:
        return False, f"ffmpeg refused the file: {result.stderr.strip()[-160:]}", None

    lowered = (result.stderr or "").lower()
    for pattern in FATAL_DECODE_PATTERNS:
        if pattern in lowered:
            return False, f"ffmpeg reported a fatal problem: {result.stderr.strip()[-160:]}", None

    decoded = None
    for match in re.finditer(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)", result.stderr or ""):
        hours, minutes, seconds = match.groups()
        decoded = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return True, "", decoded


# How much shorter than advertised a file may decode before it is treated as
# cut off. A little slack for encoder padding and for the last partial frame;
# a third of the file missing is not slack.
TRUNCATION_TOLERANCE = 0.9


def validate_audio_integrity(filepath: Path, from_outside: bool = False) -> tuple[bool, str]:
    """Integrity check for any format the library accepts.

    An .m4a that yt-dlp produced moments earlier keeps the cheap header-only
    check. Anything that arrived from outside -- an upload, a replacement
    file, a track kept from the smart-shuffle cache -- has no such provenance
    and is decoded in full, .m4a included: a faststart file with its tail
    cut off passes the header check.
    """
    if not filepath.exists():
        return False, "File does not exist"
    if filepath.stat().st_size == 0:
        return False, "File is empty"

    if filepath.suffix.lower() == ".m4a" and not from_outside:
        return validate_m4a_integrity(filepath)

    try:
        parsed = mutagen.File(filepath)
    except Exception as exc:
        return False, f"Could not parse the file: {str(exc)[:100]}"
    if parsed is None:
        return False, "Not an audio file this library understands"
    declared = getattr(getattr(parsed, "info", None), "length", 0) or 0
    if declared <= 0:
        return False, "No valid audio stream found"

    ok, message, decoded = decodes_cleanly(filepath)
    if not ok:
        return False, message

    if decoded is not None and decoded < declared * TRUNCATION_TOLERANCE:
        return False, (
            f"File looks truncated: the header claims {declared:.1f}s "
            f"but only {decoded:.1f}s decode"
        )
    return True, ""


def cleanup_old_temp_files():
    """Clean up old temporary files on startup (Problem #29)."""
    if not runtime.TMP_DIR.exists():
        return

    current_time = time.time()
    cleaned = 0

    for f in runtime.TMP_DIR.glob("*"):
        if f.is_dir():
            continue  # tmp/import — отдельно, ниже
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

    cleaned += _cleanup_stale_uploads(current_time)
    if cleaned > 0:
        logger.info(f"Cleaned {cleaned} old temp files", extra={"task_id": "system"})


def _cleanup_stale_uploads(now: float) -> int:
    """Загруженные файлы, которые уже никто не обработает.

    Файл кладётся в папку импорта ещё до проверки на дубликат, так что
    пропущенные загрузки и повторные «в фонотеку» оставляли его там навсегда.
    Убирается старше срока и только если его задача не ждёт очереди.
    """
    folder = runtime.TMP_DIR / IMPORT_DIR_NAME
    if not folder.is_dir():
        return 0
    try:
        waiting = {
            row["url"].removeprefix("file:")
            for row in db.db_query(
                "SELECT url FROM tasks WHERE url LIKE 'file:%' "
                "AND status IN ('queued', 'downloading', 'tagging')"
            )
        }
    except Exception:  # noqa: BLE001 — без базы лучше ничего не трогать
        return 0
    removed = 0
    for path in folder.iterdir():
        digest = path.name.split(".", 1)[0]
        try:
            if digest in waiting or now - path.stat().st_mtime <= runtime.TMP_TTL_SECONDS:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# Errors worth trying again. Everything else is treated as permanent, so a
# genuinely broken link is not retried three times before giving up.
RETRYABLE_ERRORS = {"network_error", "rate_limited", "download_error", "artwork_error"}

# Appended to a task's error when YouTube wants a signed-in session, since the
# fix is a setting of this service rather than anything wrong with the link.
AUTH_REQUIRED_HINT = "Set COOKIES_FROM_BROWSER in adder/.env (see README, Troubleshooting)."


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

    # A tool missing on this machine, not a problem with the video. yt-dlp says
    # "ffprobe and ffmpeg not found", which must not read as a missing video.
    if "ffmpeg not found" in text or "ffprobe not found" in text:
        return "dependency_error"

    # YouTube throttling this client. "This content isn't available, try again
    # later" is how YouTube reports a rate-limited session, so it is matched
    # before the generic "unavailable" below.
    if "http error 429" in text or "too many requests" in text or "try again later" in text:
        return "rate_limited"

    if "timeout" in text or "timed out" in text or "network" in text:
        return "network_error"
    if "connection" in text or "unreachable" in text or "temporarily" in text:
        return "network_error"
    # Only as an HTTP status: a bare "503" also matches video IDs and URLs.
    if re.search(r"http error 5\d\d", text):
        return "network_error"

    # The OS's and this service's own wording, not any "space" or "disk":
    # "namespace" in a traceback used to read as a full disk.
    if "no space left" in text or "disk space" in text or "disk full" in text:
        return "filesystem_error"
    if "database" in text or "sqlite" in text:
        return "database_error"

    if "unsupported video" in text:
        return "unsupported_video"

    # Permanently wrong link, as opposed to one that merely failed to load.
    if "invalid url" in text or "unsupported url" in text or "malformed" in text:
        return "invalid_url"
    # YouTube wants a signed-in session: bot check, age gate, members-only.
    if "not a bot" in text or "sign in to confirm" in text or "members-only" in text:
        return "youtube_auth_required"
    if "not found" in text or "unavailable" in text or "private video" in text:
        return "youtube_not_found"

    if "artwork" in text or "cover" in text:
        return "artwork_error"
    if "metadata" in text:
        return "metadata_error"
    if "download" in text:
        return "download_error"

    return "unknown_error"


def write_tags(path: Path, info, title: str, cover: bytes | None, cover_fmt: str | None) -> None:
    """Write artist, title, album, track number and cover, whatever the container.

    Each format keeps pictures somewhere different -- an MP4 atom, an ID3 APIC
    frame, a FLAC picture block, a base64 block in an Ogg comment -- so this is
    four small cases rather than one clever one.
    """
    suffix = path.suffix.lower()

    if suffix == ".m4a":
        audio = MP4(path)
        audio["\xa9nam"] = [title]
        audio["\xa9ART"] = info.artists
        audio["aART"] = [info.artists[0]]
        audio["\xa9alb"] = [info.album]
        if info.date:
            audio["\xa9day"] = [info.date]
        if info.track_number:
            audio["trkn"] = [(info.track_number, info.track_total)]
            # Deezer gives the disc a track is on, not how many the album has;
            # 0 means "unknown", where a fixed 1 made the second disc read "2/1".
            audio["disk"] = [(info.disc_number, 0)]
        if cover:
            fmt_const = MP4Cover.FORMAT_PNG if cover_fmt == "png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover, imageformat=fmt_const)]
        audio.save()
        return

    mime = "image/png" if cover_fmt == "png" else "image/jpeg"

    if suffix == ".mp3":
        from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TRCK
        from mutagen.mp3 import MP3

        mp3 = MP3(path)
        if mp3.tags is None:
            mp3.add_tags()
        tags = mp3.tags
        assert tags is not None  # add_tags() above guarantees it
        tags.setall("TIT2", [TIT2(encoding=3, text=[title])])
        tags.setall("TPE1", [TPE1(encoding=3, text=info.artists)])
        tags.setall("TPE2", [TPE2(encoding=3, text=[info.artists[0]])])
        tags.setall("TALB", [TALB(encoding=3, text=[info.album])])
        if info.date:
            tags.setall("TDRC", [TDRC(encoding=3, text=[info.date])])
        if info.track_number:
            numbering = f"{info.track_number}/{info.track_total}"
            tags.setall("TRCK", [TRCK(encoding=3, text=[numbering])])
        if cover:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
        mp3.save()
        return

    if suffix == ".flac":
        from mutagen.flac import FLAC, Picture

        flac = FLAC(path)
        flac["title"] = [title]
        flac["artist"] = info.artists
        flac["albumartist"] = [info.artists[0]]
        flac["album"] = [info.album]
        if info.date:
            flac["date"] = [info.date]
        if info.track_number:
            flac["tracknumber"] = [str(info.track_number)]
        if cover:
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, mime, cover
            flac.clear_pictures()
            flac.add_picture(picture)
        flac.save()
        return

    # Ogg and Opus: text tags are plain comments, the cover is a base64 FLAC
    # picture block, which is the convention every player expects here.
    ogg = mutagen.File(path)
    if ogg is None:
        raise RuntimeError(f"Cannot tag {path.name}: unrecognised format")
    ogg["title"] = [title]
    ogg["artist"] = list(info.artists)
    ogg["albumartist"] = [info.artists[0]]
    ogg["album"] = [info.album]
    if info.date:
        ogg["date"] = [info.date]
    if info.track_number:
        ogg["tracknumber"] = [str(info.track_number)]
    if cover:
        import base64

        from mutagen.flac import Picture

        picture = Picture()
        picture.type, picture.mime, picture.data = 3, mime, cover
        ogg["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    ogg.save()


SOURCE_TAG = "SOURCE_URL"


def write_source(path: Path, url: str) -> None:
    """Record where a track came from inside the file itself.

    The link lived only in the task table, so losing adder.db lost the
    connection between a file and its video: no re-download, no "open on
    YouTube", no way to tell a re-submitted link was already here. A freeform
    tag travels with the file. Navidrome ignores it.
    """
    suffix = path.suffix.lower()
    if suffix == ".m4a":
        from mutagen.mp4 import MP4FreeForm

        audio = MP4(path)
        audio[f"----:com.apple.iTunes:{SOURCE_TAG}"] = [MP4FreeForm(url.encode("utf-8"))]
        audio.save()
    elif suffix == ".mp3":
        from mutagen.id3 import TXXX
        from mutagen.mp3 import MP3

        mp3 = MP3(path)
        if mp3.tags is None:
            mp3.add_tags()
        assert mp3.tags is not None
        mp3.tags.setall(f"TXXX:{SOURCE_TAG}", [TXXX(encoding=3, desc=SOURCE_TAG, text=[url])])
        mp3.save()
    else:
        other = mutagen.File(path)
        if other is None:
            return
        other[SOURCE_TAG.lower()] = [url]
        other.save()


def edit_track(
    path: Path, title: str, artists: list[str], album: str, refetch_cover: bool = False
) -> str:
    """Correct a track's tags by hand. Returns what happened to the cover.

    YouTube metadata is often a little wrong and Deezer sometimes matches the
    wrong release; until now fixing that took a separate tag editor. The file
    stays where it is: Navidrome groups by tags, not folders.

    The modification time is kept. The library reads it as "when this track
    arrived", and correcting a typo is not an arrival.
    """
    runtime.guard_real_library(config.LIBRARY, "edit tags")
    title, album = title.strip(), album.strip()
    artists = [a.strip() for a in artists if a and a.strip()]
    if not title or not artists:
        raise ValueError("Title and at least one artist are required")

    cover, fmt, cover_result = None, None, "kept"
    if refetch_cover:
        info = enrich.lookup(artists[0], title)
        if info and info.cover_url:
            cover, fmt = fetch_cover_url(info.cover_url)
        if not cover:
            cover, fmt = get_hd_cover(artists[0], title)
        cover_result = "updated" if cover else "not found"

    stat = path.stat()
    write_tags(path, enrich.TrackInfo(album=album or title, artists=artists), title, cover, fmt)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    library.invalidate_library_index()
    return cover_result


def verify_tags_written(path: Path, expected_title: str) -> bool:
    """Read the title back out. A save that silently did nothing is a real failure mode."""
    try:
        return bool(library.read_tags(path).get("title"))
    except Exception:
        return False


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
    place where tagging and duplicate checks are allowed to happen. The suffix
    is carried over: a downloaded track is always .m4a, an imported one keeps
    whatever it arrived as.
    """
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".m4a"
    temp_path = runtime.TMP_DIR / f"{key}_processing{suffix}"
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
    check_downloadable(meta)
    fs_artist, fs_title, full_artist, meta_title = split_artist_title(meta)

    downloaded = yt_download(url, meta["id"])
    if not downloaded.exists():
        raise RuntimeError("Downloaded file not found")

    is_valid, error_msg = validate_audio_integrity(downloaded)
    if not is_valid:
        # Not staged yet, so process() knows nothing to clean up: without this
        # a broken download sat in tmp/ until the 24-hour sweep.
        downloaded.unlink(missing_ok=True)
        raise RuntimeError(error_msg)

    return Downloaded(
        temp_path=stage_into_temp(downloaded, meta["id"]),
        names=TrackNames(fs_artist, fs_title, full_artist, meta_title),
        thumbnail=meta.get("thumbnail"),
    )


IMPORT_DIR_NAME = "import"


def import_dir() -> Path:
    """Where an uploaded file waits between the request and its turn in the queue."""
    path = runtime.TMP_DIR / IMPORT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def stash_upload(data: bytes, original_name: str) -> tuple[str, Path]:
    """Save an uploaded file and return the source key it will be queued under.

    Tasks are keyed by URL and the column is unique, which is what makes
    resubmitting a link idempotent. A local file has no URL, so it gets a
    synthetic one built from its own content: uploading the same bytes twice is
    the same task, exactly as pasting the same link twice is.
    """
    suffix = Path(original_name).suffix.lower() or ".mp3"
    digest = hashlib.sha256(data).hexdigest()
    target = import_dir() / f"{digest}{suffix}"
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    partial.replace(target)
    # Настоящее имя файла — рядом: сам файл назван по содержимому, и без этого
    # «Кино - Группа крови.mp3» без тегов ложился в фонотеку под хешем.
    target.with_name(f"{digest}{NAME_SUFFIX}").write_text(
        Path(original_name).name, encoding="utf-8"
    )
    return f"file:{digest}", target


NAME_SUFFIX = ".name"


def stashed_upload(source_key: str) -> Path | None:
    digest = source_key.removeprefix("file:")
    matches = sorted(
        path
        for path in import_dir().glob(f"{digest}.*")
        if path.suffix.lower() in library.AUDIO_SUFFIXES
    )
    return matches[0] if matches else None


def stashed_name(source_key: str) -> str | None:
    """Имя, под которым файл загрузили, если оно сохранилось."""
    digest = source_key.removeprefix("file:")
    try:
        return (import_dir() / f"{digest}{NAME_SUFFIX}").read_text(encoding="utf-8") or None
    except OSError:
        return None


def forget_upload(source_key: str) -> None:
    """Убрать загруженный файл и его имя — после того как задача закончилась."""
    digest = source_key.removeprefix("file:")
    for path in import_dir().glob(f"{digest}.*"):
        path.unlink(missing_ok=True)


def import_local_file(tid: int, source: Path, original_name: str) -> Downloaded:
    """Take a file the user dropped in and get it ready for the library half.

    The counterpart to download_to_temp: a different way of obtaining bytes,
    handing over the same thing. Names come from the file's own tags where it
    has them -- a file that arrives already tagged knows better than its
    filename does -- and from the filename where it does not.
    """
    has_space, free_mb = check_disk_space()
    if not has_space:
        raise RuntimeError(
            f"Insufficient disk space: {free_mb}MB free, {config.MIN_FREE_SPACE_MB}MB required"
        )

    db.task_update(tid, status="downloading")

    is_valid, error_msg = validate_audio_integrity(source, from_outside=True)
    if not is_valid:
        raise RuntimeError(error_msg)

    try:
        tags = library.read_tags(source)
    except Exception:
        tags = {}

    artist = (tags.get("artist") or "").strip()
    title = (tags.get("title") or "").strip()
    # read_tags falls back to the file stem so the panel always has something
    # to show. Here that fallback is in the way: a stem is not a title, and
    # treating it as one would leave "Кино - Группа крови" as the track name
    # with no artist ever split out of it.
    if title == source.stem or title == Path(original_name).stem:
        title = ""
    if not artist or not title:
        # "Artist - Title.mp3" is the one filename convention worth reading.
        stem = Path(original_name).stem
        if " - " in stem:
            guessed_artist, guessed_title = stem.split(" - ", 1)
        else:
            guessed_artist, guessed_title = artist or "Unknown Artist", stem
        artist = artist or guessed_artist.strip()
        title = title or guessed_title.strip()

    names = TrackNames(
        fs_artist=sanitize_filename(artist),
        fs_title=sanitize_filename(clean_title(title, for_filename=True)),
        full_artist=artist,
        meta_title=clean_title(title, for_filename=False),
    )
    key = file_sha256(source)[:16]
    # Копия, а не перенос: загруженный файл остаётся в папке импорта, пока
    # задача не закончится. Перенос терял его при перезапуске посреди
    # обработки и при повторной попытке — задача возвращалась, а файла нет.
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    staged = runtime.TMP_DIR / f"{key}_processing{source.suffix.lower() or '.m4a'}"
    shutil.copy2(source, staged)
    return Downloaded(temp_path=staged, names=names, thumbnail=None)


def _apply_replacement(tid: int, new_path: str) -> None:
    """Поставить свежескачанный трек на место того, который он заменяет.

    Заменяем не байты файла, а ссылку: новый трек проходит обычный конвейер со
    своими тегами и своим именем, а потом занимает место старого во всех
    подборках. Перезапись байтов сохранила бы путь, но развела бы имя файла с
    содержимым — и расширение могло не совпасть.

    Старый файл уезжает в корзину, а не стирается: «загрузилась не та версия» —
    это ровно тот случай, когда через день выясняется, что та.
    """
    rows = db.db_query("SELECT replace_of FROM tasks WHERE id = ?", (tid,))
    old_path = (rows[0]["replace_of"] if rows else None) or ""
    if not old_path or old_path == new_path:
        return

    from . import playlists

    moved = playlists.swap_everywhere(old_path, new_path)
    try:
        # Тот же путь, что и у обычного удаления: файл уезжает в корзину, а
        # пустые папки за ним подчищаются.
        library.delete_track(old_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Не удалось убрать заменённый файл %s: %s", old_path, exc, extra={"task_id": tid}
        )
    logger.info(
        "Замена: %s → %s, подборок затронуто %d",
        old_path,
        new_path,
        moved,
        extra={"task_id": tid},
    )


def _task_source(tid: int) -> str | None:
    """The link a task was queued for, if it is a web link (uploads are not)."""
    rows = db.db_query("SELECT url FROM tasks WHERE id = ?", (tid,))
    url = rows[0]["url"] if rows else None
    return url if url and url.startswith(("http://", "https://")) else None


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
    hint = db.db_query("SELECT album_hint FROM tasks WHERE id = ?", (tid,))
    album_id = hint[0]["album_hint"] if hint else None
    info, source = enrich.describe(names.full_artist, names.meta_title, album_id)
    logger.info(
        "Metadata for %r by %r: album=%r track=%s source=%s",
        names.meta_title,
        names.full_artist,
        info.album,
        info.track_number,
        source,
    )

    cover, fmt = None, None
    if info.cover_url:
        cover, fmt = fetch_cover_url(info.cover_url)
    if not cover:
        cover, fmt = fetch_cover(names.fs_artist, names.fs_title, thumbnail)

    # Folder layout is left alone on purpose: Navidrome groups albums by
    # tags, not by directory, so moving files would buy nothing. The folder
    # itself is created under the lock right before the move: deleting the
    # artist's last track meanwhile removes it.
    target_dir = config.LIBRARY / names.fs_artist / "Singles"
    # An imported file keeps its own container; nothing is re-encoded to make
    # the folder uniform, because that would cost quality for tidiness.
    base_target = target_dir / f"{names.fs_title}{temp_path.suffix.lower()}"

    write_tags(temp_path, info, names.meta_title, cover, fmt)
    # Not "source": that name already holds where the metadata came from.
    source_url = _task_source(tid)
    if source_url:
        write_source(temp_path, source_url)
    # Loudness, so a shuffle does not jump between quiet and loud uploads. A
    # failed measurement only means no ReplayGain tag, never a failed task.
    try:
        loudness.apply(temp_path)
    except Exception as exc:
        logger.warning("ReplayGain not written: %s", exc, extra={"task_id": tid})

    if not verify_tags_written(temp_path, names.meta_title):
        raise RuntimeError("Metadata write failed verification")

    is_valid, error_msg = validate_audio_integrity(temp_path)
    if not is_valid:
        raise RuntimeError(f"Final validation failed: {error_msg}")

    # Outside the lock: the library index may read every file's tags, and a
    # race here costs at most a missed warning, never a wrong file.
    similar = find_similar_library_track(info.artists, names.meta_title, audio_duration(temp_path))

    # Content-based duplicate detection must happen while holding the same
    # lock as the final move. This prevents concurrent workers from both
    # accepting identical audio.
    with runtime.FILE_LOCK:
        target_dir.mkdir(parents=True, exist_ok=True)
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

    # Measure it now rather than waiting for the next full pass, so a track
    # added today can be placed by tempo tonight. It runs in its own bounded
    # scope, not in this process.
    from . import analysis

    analysis.analyse_track(str(final_target))

    # Куда лёг файл — нужно для замены трека: подмену нельзя искать по артисту
    # и названию, иначе в корзину уедет не тот.
    relative = str(final_target.relative_to(config.LIBRARY.resolve()))
    db.task_update(tid, result_path=relative)
    if similar:
        # Kept on purpose: it may be a better recording, and which one to keep
        # is the listener's call. The panel shows this next to the task.
        logger.warning(
            "Stored %s, but the library already has a similar track: %s",
            relative,
            similar,
            extra={"task_id": tid},
        )
        db.task_update(tid, warning=f"Похоже на уже имеющийся трек: {similar}", similar_to=similar)
    _apply_replacement(tid, relative)

    # Текст — сразу, чтобы он был уже при первом включении. В своём потоке:
    # на промахе это несколько запросов к чужому каталогу, и ждать их незачем
    # ни очереди скачиваний, ни остановке службы. Не вышло — трек подберёт
    # обход фонотеки.
    threading.Thread(
        target=_lyrics_for_new_track, args=(relative,), name="lyrics-new", daemon=True
    ).start()
    return "stored"


def _lyrics_for_new_track(relative: str) -> None:
    from . import lyrics

    try:
        lyrics.for_track(relative)
    except Exception as exc:  # noqa: BLE001 — текст не стоит упавшего потока
        logger.info("Текст для нового трека не получен: %s", exc)
