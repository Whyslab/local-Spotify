import os
import sys
from pathlib import Path

# from adder.app import ... below is a package-qualified import, which
# needs the project root (parent of tests/) on sys.path. This file used
# to rely entirely on an external PYTHONPATH being set - true when run
# via the README's documented `PYTHONPATH="$PWD" pytest -q`, but ci.yml's
# "Run tests" step never sets PYTHONPATH, so a bare `pytest -q` (exactly
# what CI runs) failed collection with "ModuleNotFoundError: No module
# named 'adder'" before a single test could execute. Every other file in
# this suite already does its own sys.path bootstrap (see test_app.py,
# test_config.py) - this makes the import self-contained the same way,
# instead of depending on how the caller invokes pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("API_TOKEN", "test-secret")

from adder.app import clean_title


def test_empty_parentheses_are_removed():
    assert clean_title(
        "Get Lucky (Official Video) feat. Pharrell Williams and Nile Rodgers",
        for_filename=True,
    ) == "Get Lucky feat. Pharrell Williams and Nile Rodgers"


def test_empty_brackets_are_removed():
    assert clean_title(
        "Track [Official Audio] feat. Artist",
        for_filename=True,
    ) == "Track feat. Artist"


def test_normal_title_is_preserved():
    assert clean_title(
        "Get Lucky",
        for_filename=True,
    ) == "Get Lucky"


def test_version_information_is_removed_from_filename():
    assert clean_title(
        "Song (Official Video) (Live)",
        for_filename=True,
    ) == "Song"


def test_metadata_keeps_version_information():
    assert clean_title(
        "Song (Official Video) (Live)",
        for_filename=False,
    ) == "Song (Live)"
