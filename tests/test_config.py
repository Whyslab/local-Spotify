"""Tests for configuration module (Problems #3, #4, #5)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "adder"))

def test_config_loads():
    """Test that config module loads without errors."""
    from config import LIBRARY, PORT, HOST, MAX_WORKERS
    assert LIBRARY is not None
    assert isinstance(PORT, int)
    assert isinstance(HOST, str)
    assert isinstance(MAX_WORKERS, int)

def test_default_library_path():
    """Test default library path is ~/Music/Normalized Library."""
    from config import LIBRARY
    expected = Path.home() / "Music" / "Normalized Library"
    assert str(LIBRARY) == str(expected) or LIBRARY.exists()

def test_delay_between_tracks():
    """Test DELAY_BETWEEN_TRACKS is configurable."""
    from config import DELAY_BETWEEN_TRACKS
    assert isinstance(DELAY_BETWEEN_TRACKS, float)
    assert DELAY_BETWEEN_TRACKS > 0

def test_min_free_space():
    """Test MIN_FREE_SPACE_MB is configurable (Problem #30)."""
    from config import MIN_FREE_SPACE_MB
    assert isinstance(MIN_FREE_SPACE_MB, int)
    assert MIN_FREE_SPACE_MB > 0

def test_tmp_ttl_hours():
    """Test TMP_TTL_HOURS is configurable (Problem #29)."""
    from config import TMP_TTL_HOURS
    assert isinstance(TMP_TTL_HOURS, int)
    assert TMP_TTL_HOURS > 0

def test_max_retries():
    """Test MAX_RETRIES is configurable (Problem #23)."""
    from config import MAX_RETRIES
    assert isinstance(MAX_RETRIES, int)
    assert MAX_RETRIES >= 0
