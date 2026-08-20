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
LIBRARY = Path(os.environ.get(
    "LIBRARY_PATH",
    str(Path.home() / "Music" / "Normalized Library")
))

PORT = int(os.environ.get("PORT", "8787"))

HOST = os.environ.get("HOST", "0.0.0.0")

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))

DELAY_BETWEEN_TRACKS = float(os.environ.get("DELAY_BETWEEN_TRACKS", "1.1"))

# Queue and request limits (Problem #9)
MAX_LINKS_PER_REQUEST = int(os.environ.get("MAX_LINKS_PER_REQUEST", "100"))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "5000"))

# YouTube matching settings (Problem #11)
YT_SEARCH_COUNT = int(os.environ.get("YT_SEARCH_COUNT", "5"))
YT_MATCH_MIN_SCORE = float(os.environ.get("YT_MATCH_MIN_SCORE", "0.5"))

# Metadata settings (Problems #12, #13)
PRESERVE_FEAT_ARTISTS = os.environ.get("PRESERVE_FEAT_ARTISTS", "true").lower() == "true"

# API Authentication (Problem #19)
API_TOKEN = os.environ.get("API_TOKEN", "")

# Retry settings (Problem #23)
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.environ.get("RETRY_BACKOFF_BASE", "2.0"))

# Graceful shutdown timeout (Problem #22)
SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", "30"))

# Disk space check (Problem #30)
MIN_FREE_SPACE_MB = int(os.environ.get("MIN_FREE_SPACE_MB", "2048"))

# Temporary file TTL in hours (Problem #29)
TMP_TTL_HOURS = int(os.environ.get("TMP_TTL_HOURS", "24"))
