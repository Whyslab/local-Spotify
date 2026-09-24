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

import pytest

from adder.ingest import MAX_NAME_BYTES, clean_title, sanitize_filename, split_artist_title


def test_empty_parentheses_are_removed():
    assert (
        clean_title(
            "Get Lucky (Official Video) feat. Pharrell Williams and Nile Rodgers",
            for_filename=True,
        )
        == "Get Lucky feat. Pharrell Williams and Nile Rodgers"
    )


def test_empty_brackets_are_removed():
    assert (
        clean_title(
            "Track [Official Audio] feat. Artist",
            for_filename=True,
        )
        == "Track feat. Artist"
    )


def test_normal_title_is_preserved():
    assert (
        clean_title(
            "Get Lucky",
            for_filename=True,
        )
        == "Get Lucky"
    )


def test_version_information_is_removed_from_filename():
    assert (
        clean_title(
            "Song (Official Video) (Live)",
            for_filename=True,
        )
        == "Song"
    )


def test_metadata_keeps_version_information():
    assert (
        clean_title(
            "Song (Official Video) (Live)",
            for_filename=False,
        )
        == "Song (Live)"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("это любовь - текст", "это любовь"),
        ("Мой текст", "Мой текст"),
        ("Fata Morgana (текст)", "Fata Morgana"),
        ("Money Flow [Lyrics]", "Money Flow"),
        ("Song + lyrics", "Song"),
        ("Бывшая (Audio)", "Бывшая"),
        ("Текст", "Текст"),
    ],
)
def test_lyric_video_marks_are_not_part_of_the_title(raw, expected):
    """Первым в поиске часто стоит ролик с текстом — пометка не должна стать
    названием: по нему потом не находится ни текст, ни дубликат."""
    assert clean_title(raw, for_filename=False) == expected


# ---------------------------------------------------------------------------
# sanitize_filename: channel names and titles are untrusted input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["..", ".", "...", " .. "])
def test_dot_names_cannot_step_out_of_the_library(name):
    assert sanitize_filename(name) == "Unknown"


def test_leading_dot_would_hide_the_track_from_navidrome():
    assert sanitize_filename(".hidden") == "hidden"


@pytest.mark.parametrize("name", ["", "?", '""', "***", "\x00\x01"])
def test_names_with_nothing_safe_left_become_unknown(name):
    assert sanitize_filename(name) == "Unknown"


def test_trailing_dots_are_kept_so_existing_folders_still_match():
    assert sanitize_filename("R.E.M.") == "R.E.M."


def test_long_names_fit_the_filesystem_without_splitting_characters():
    name = "🎵" * 100 + "Я" * 100  # 600 bytes of UTF-8

    result = sanitize_filename(name)

    assert len(result.encode("utf-8")) <= MAX_NAME_BYTES
    assert result.startswith("🎵")


def test_malicious_channel_name_stays_one_path_component():
    fs_artist, fs_title, _, _ = split_artist_title({"title": "Song", "uploader": ".."})

    assert fs_artist == "Unknown"
    assert "/" not in fs_title
