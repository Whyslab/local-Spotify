"""Metadata: reading YouTube's fields and enriching them from Deezer.

Deezer is never contacted: enrich._get, the single function that performs
HTTP requests, is replaced with canned API responses.
"""

import pytest

from adder import enrich
from adder.ingest import split_artist_title

# conftest replaces enrich.lookup in every test so nothing reaches Deezer;
# these tests exercise the real function against a faked _get instead.
lookup = enrich.lookup

SEARCH = {
    "data": [
        {"id": 1, "title": "Get Lucky (Radio Edit)"},
        {"id": 2, "title": "Get Lucky (feat. Pharrell Williams)"},
    ]
}
TRACK = {
    "id": 2,
    "track_position": 8,
    "disk_number": 1,
    "release_date": "2013-05-17",
    "contributors": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}],
    "album": {"id": 77, "title": "Random Access Memories", "cover_xl": "https://img/xl.jpg"},
}
ALBUM = {"id": 77, "nb_tracks": 13}


def deezer(responses):
    """A fake _get answering by URL prefix; unknown URLs behave like a network failure."""
    requested = []

    def fake_get(url):
        requested.append(url)
        for prefix, body in responses.items():
            if url.startswith(f"{enrich.DEEZER}{prefix}"):
                return body
        return None

    fake_get.requested = requested
    return fake_get


# ---------------------------------------------------------------------------
# Deezer lookup
# ---------------------------------------------------------------------------


def test_lookup_fills_album_position_artists_and_cover(monkeypatch):
    fake = deezer({"/search": SEARCH, "/track/2": TRACK, "/album/77": ALBUM})
    monkeypatch.setattr(enrich, "_get", fake)

    info = lookup("Daft Punk feat. Pharrell Williams", "Get Lucky")

    assert info == enrich.TrackInfo(
        album="Random Access Memories",
        artists=["Daft Punk", "Pharrell Williams"],
        track_number=8,
        track_total=13,
        disc_number=1,
        date="2013-05-17",
        cover_url="https://img/xl.jpg",
    )
    # The search is scoped to the lead artist only.
    assert "Daft%20Punk" in fake.requested[0]
    assert "Pharrell" not in fake.requested[0]


def test_lookup_rejects_a_different_version_of_the_title(monkeypatch):
    search = {"data": [{"id": 1, "title": "Get Lucky (Radio Edit)"}]}
    monkeypatch.setattr(enrich, "_get", deezer({"/search": search}))

    assert lookup("Daft Punk", "Get Lucky") is None


def test_lookup_survives_a_missing_album_detail(monkeypatch):
    monkeypatch.setattr(enrich, "_get", deezer({"/search": SEARCH, "/track/2": TRACK}))

    info = lookup("Daft Punk", "Get Lucky")

    assert info.album == "Random Access Memories"
    assert info.track_total == 0


@pytest.mark.parametrize(
    "responses",
    [
        {},  # network down: every request fails
        {"/search": {"data": []}},  # nothing found
        {"/search": SEARCH},  # track detail request failed
    ],
)
def test_lookup_returns_none_rather_than_raising(monkeypatch, responses):
    monkeypatch.setattr(enrich, "_get", deezer(responses))

    assert lookup("Daft Punk", "Get Lucky") is None


def test_describe_falls_back_to_a_single_named_after_the_track(monkeypatch):
    monkeypatch.setattr(enrich, "_get", deezer({}))

    info, from_deezer = enrich.describe("Artist A, Artist B", "Some Song")

    assert from_deezer is False
    assert info.album == "Some Song"
    assert info.artists == ["Artist A", "Artist B"]
    assert info.track_number is None


def test_get_returns_none_when_the_network_fails(monkeypatch):
    def unreachable(*args, **kwargs):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(enrich.urllib.request, "urlopen", unreachable)

    assert enrich._get(f"{enrich.DEEZER}/search?q=x") is None


@pytest.mark.parametrize(
    ("joined", "expected"),
    [
        ("A, B & C", ["A", "B", "C"]),
        ("A feat. B", ["A", "B"]),
        ("A ft B", ["A", "B"]),
        ("A x B", ["A", "B"]),
        ("A; a", ["A"]),
        ("", []),
    ],
)
def test_split_artists(joined, expected):
    assert enrich.split_artists(joined) == expected


def test_normalize_ignores_case_feat_and_punctuation():
    assert enrich.normalize("Get Lucky (feat. Pharrell)") == enrich.normalize("GET LUCKY!")


# ---------------------------------------------------------------------------
# YouTube fields -> artist and title
# ---------------------------------------------------------------------------


def test_music_fields_win_over_the_video_title():
    meta = {"artist": "Artist", "track": "Song", "title": "Artist - Song (Official Video)"}

    fs_artist, fs_title, full_artist, meta_title = split_artist_title(meta)

    assert (fs_artist, fs_title, full_artist, meta_title) == ("Artist", "Song", "Artist", "Song")


def test_artist_is_split_off_a_dashed_title():
    meta = {"title": "Artist - Song (Official Music Video)", "uploader": "Label"}

    fs_artist, fs_title, full_artist, meta_title = split_artist_title(meta)

    assert full_artist == "Artist"
    assert fs_title == "Song"
    assert meta_title == "Song"


def test_version_stays_in_tags_but_not_in_the_filename():
    meta = {"title": "Artist - Song (Live)", "uploader": "x"}

    _, fs_title, _, meta_title = split_artist_title(meta)

    assert fs_title == "Song"
    assert meta_title == "Song (Live)"


def test_uploader_is_the_artist_of_last_resort():
    fs_artist, _, full_artist, meta_title = split_artist_title(
        {"title": "Song", "uploader": "Chan"}
    )

    assert (fs_artist, full_artist, meta_title) == ("Chan", "Chan", "Song")


def test_missing_everything_does_not_crash():
    fs_artist, fs_title, full_artist, meta_title = split_artist_title({})

    assert fs_artist == "Unknown Artist"
    assert fs_title == "Unknown"
    assert meta_title == "Unknown"
