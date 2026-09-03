"""Unified configuration module for local-Spotify project.

Loads environment variables from .env file and provides type-safe access
to configuration values with sensible defaults.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the adder directory
_dotenv_path = Path(__file__).parent / ".env"
load_dotenv(_dotenv_path)

# Configuration values with defaults


def _positive_int(name: str, default: str) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _nonnegative_int(name: str, default: str) -> int:
    value = int(os.environ.get(name, default))
    if value < 0:
        raise RuntimeError(f"{name} must not be negative")
    return value


def _positive_float(name: str, default: str) -> float:
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


LIBRARY = Path(os.environ.get("LIBRARY_PATH", str(Path.home() / "Music" / "Normalized Library")))

PORT = _positive_int("PORT", "8787")

HOST = os.environ.get("HOST", "0.0.0.0")

MAX_WORKERS = _positive_int("MAX_WORKERS", "2")

DELAY_BETWEEN_TRACKS = _positive_float("DELAY_BETWEEN_TRACKS", "1.1")

# Queue and request limits (Problem #9)
MAX_LINKS_PER_REQUEST = _positive_int("MAX_LINKS_PER_REQUEST", "100")
MAX_QUEUE_SIZE = _positive_int("MAX_QUEUE_SIZE", "5000")

# Metadata settings (Problems #12, #13)
PRESERVE_FEAT_ARTISTS = os.environ.get("PRESERVE_FEAT_ARTISTS", "true").lower() == "true"

# API Authentication (Problem #19)
# The API is reachable from the LAN, so an empty token must fail closed.
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
if not API_TOKEN:
    raise RuntimeError(
        "API_TOKEN is required. Set a strong random token in adder/.env "
        "or the systemd environment before starting local-Spotify."
    )

# Retry settings (Problem #23)
MAX_RETRIES = _nonnegative_int("MAX_RETRIES", "3")
RETRY_BACKOFF_BASE = _positive_float("RETRY_BACKOFF_BASE", "2.0")

# Graceful shutdown timeout (Problem #22)
SHUTDOWN_TIMEOUT = _positive_int("SHUTDOWN_TIMEOUT", "30")

# Disk space check (Problem #30)
MIN_FREE_SPACE_MB = _positive_int("MIN_FREE_SPACE_MB", "2048")

# Temporary file TTL in hours (Problem #29)
TMP_TTL_HOURS = _positive_int("TMP_TTL_HOURS", "24")

# yt-dlp cookie source (optional).
# YouTube sometimes demands proof that a request is not a bot, and refuses the
# download outright. Passing a logged-in browser's cookies satisfies that check.
# Format is yt-dlp's own: "firefox", "chrome", or "firefox:/path/to/profile"
# (needed here because this machine keeps Firefox profiles under ~/.config).
# Empty means no cookies, which is the right default for anyone not hitting it.
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "").strip()

# Navidrome, for the things only it can hold.
#
# Playlist covers live in its database -- there is nowhere else to put them
# that a Subsonic client will read -- and a playlist whose .m3u disappears has
# to be deleted through its API, because it does not notice the file is gone.
# Both are optional: without credentials the service still runs, playlists
# still work, and the Navidrome side of the work is queued until it can be
# delivered. Losing covers is not a reason to refuse to start.
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "").strip()
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")

# Playlist cover uploads. 8 MB clears a phone photo comfortably, and Navidrome
# re-encodes anyway under its own coverArtQuality setting.
MAX_COVER_BYTES = _positive_int("MAX_COVER_BYTES", str(8 * 1024 * 1024))

# How long the play journal is kept. A year plus a margin, so a year-on-year
# comparison still has both ends.
PLAY_HISTORY_DAYS = _positive_int("PLAY_HISTORY_DAYS", "400")
