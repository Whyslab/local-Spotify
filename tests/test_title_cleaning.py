import os

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
