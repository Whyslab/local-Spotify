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
