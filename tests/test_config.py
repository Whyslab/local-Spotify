"""Tests for configuration module (Problems #3, #4, #5)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "adder"))

# config.py raises RuntimeError at import time if API_TOKEN is unset. This
# file used to depend on another test file setting it first and being
# collected earlier - harmless in the full suite, but it meant running
# `pytest tests/test_config.py` alone failed with an unrelated-looking
# RuntimeError. setdefault so a real API_TOKEN already in the environment
# is never overridden.
os.environ.setdefault("API_TOKEN", "test-secret")


def test_config_loads():
    """Test that config module loads without errors."""
    from config import HOST, LIBRARY, MAX_WORKERS, PORT

    assert LIBRARY is not None
    assert isinstance(PORT, int)
    assert isinstance(HOST, str)
    assert isinstance(MAX_WORKERS, int)


def test_default_library_path():
    """Test LIBRARY resolves LIBRARY_PATH when set, else ~/Music/Normalized Library.

    The previous version of this assertion (str(LIBRARY) == str(expected)
    or LIBRARY.exists()) only passed if LIBRARY_PATH was unset, or if it
    happened to point at a directory that already existed on whatever
    machine ran the tests. ci.yml sets LIBRARY_PATH to a workspace
    subfolder it never creates, so this failed in the real GitHub Actions
    run even after test_health_does_not_require_auth and the XSS
    regression test were fixed. Test the documented precedence directly
    instead of depending on the filesystem.
    """
    from config import LIBRARY

    override = os.environ.get("LIBRARY_PATH")
    expected = Path(override) if override else Path.home() / "Music" / "Normalized Library"
    assert expected == LIBRARY


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
