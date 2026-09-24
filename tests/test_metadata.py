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

    monkeypatch.setattr(enrich, "musicbrainz_lookup", lambda artist, title: None)
    info, source = enrich.describe("Artist A, Artist B", "Some Song")

    assert source == "fallback"
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


@pytest.mark.parametrize(
    ("uploader", "artist"),
    [
        ("Adele - Topic", "Adele"),
        ("AdeleVEVO", "Adele"),
        ("Adele VEVO", "Adele"),
        ("Small Band", "Small Band"),
        ("Vevo", "Unknown Artist"),
    ],
)
def test_channel_suffixes_are_not_part_of_the_artist(uploader, artist):
    _, _, full_artist, _ = split_artist_title({"title": "Song", "uploader": uploader})

    assert full_artist == artist


@pytest.mark.parametrize(
    ("artist", "folder"),
    [
        ("Daft Punk feat. Pharrell", "Daft Punk"),
        ("Daft Punk ft. Pharrell", "Daft Punk"),
        ("Daft Punk ft Pharrell", "Daft Punk"),
        ("Daft Punk Featuring Pharrell", "Daft Punk"),
        ("A, B", "A"),
        ("Joe Feather", "Joe Feather"),
        ("Lefty Ftw", "Lefty Ftw"),
    ],
)
def test_lead_artist_folder_splits_on_whole_words_only(monkeypatch, artist, folder):
    from adder import config

    monkeypatch.setattr(config, "PRESERVE_FEAT_ARTISTS", False)

    fs_artist, _, full_artist, _ = split_artist_title({"artist": artist, "track": "Song"})

    assert fs_artist == folder
    assert full_artist == artist


def test_artist_is_not_repeated_in_the_title_when_track_is_missing():
    meta = {"artist": "Adele", "title": "Adele - Hello (Official Video)"}

    fs_artist, fs_title, full_artist, meta_title = split_artist_title(meta)

    assert (full_artist, meta_title, fs_title) == ("Adele", "Hello", "Hello")


def test_a_dash_that_is_part_of_the_title_is_kept():
    meta = {"artist": "Adele", "title": "Someone - Like You"}

    _, _, _, meta_title = split_artist_title(meta)

    assert meta_title == "Someone - Like You"


# ---------------------------------------------------------------------------
# MusicBrainz, when Deezer does not know the track
# ---------------------------------------------------------------------------

# conftest stubs this out for every test; the tests below use the real one.
musicbrainz_lookup = enrich.musicbrainz_lookup


def recording(title, artists, releases, score=100):
    return {
        "score": score,
        "title": title,
        "artist-credit": [{"name": a} for a in artists],
        "releases": releases,
    }


def release(
    mbid, title, kind="Album", status="Official", date="2001-01-01", secondary=None, **media
):
    return {
        "id": mbid,
        "title": title,
        "status": status,
        "date": date,
        "release-group": {"primary-type": kind, "secondary-types": secondary or []},
        "media": [
            {
                "position": media.get("disc", 1),
                "track-count": media.get("total", 12),
                "track": [
                    {"number": str(media.get("number", 3)), "position": media.get("number", 3)}
                ],
            }
        ],
    }


def test_musicbrainz_prefers_the_official_original_album(monkeypatch):
    answer = {
        "recordings": [
            recording(
                "Karma Police",
                ["Radiohead"],
                [
                    release("c", "Greatest Hits", secondary=["Compilation"], date="2008"),
                    release("s", "Karma Police", kind="Single", date="1997-08-25"),
                    release("a", "OK Computer", date="1997-05-21", number=6, total=12),
                ],
            )
        ]
    }
    requested = []
    monkeypatch.setattr(enrich, "_mb_get", lambda url: requested.append(url) or answer)

    info = musicbrainz_lookup("Radiohead", "Karma Police")

    assert info.album == "OK Computer"
    assert (info.track_number, info.track_total, info.disc_number) == (6, 12, 1)
    assert info.date == "1997-05-21"
    assert info.artists == ["Radiohead"]
    assert info.cover_url == "https://coverartarchive.org/release/a/front-500"
    assert "recording%3A%22Karma%20Police%22" in requested[0]


@pytest.mark.parametrize(
    "found",
    [
        {"recordings": [recording("Karma Police", ["Radiohead"], [release("a", "X")], score=40)]},
        {"recordings": [recording("Karma Police (Live)", ["Radiohead"], [release("a", "X")])]},
        {"recordings": [recording("Karma Police", ["A Tribute Band"], [release("a", "X")])]},
        {"recordings": [recording("Karma Police", ["Radiohead"], [])]},
        {"recordings": []},
        None,  # network down
    ],
)
def test_musicbrainz_rejects_what_is_not_this_song(monkeypatch, found):
    monkeypatch.setattr(enrich, "_mb_get", lambda url: found)

    assert musicbrainz_lookup("Radiohead", "Karma Police") is None


def test_describe_asks_musicbrainz_only_after_deezer_missed(monkeypatch):
    mb = enrich.TrackInfo(album="From MB", artists=["A"])
    monkeypatch.setattr(enrich, "musicbrainz_lookup", lambda a, t: mb)

    monkeypatch.setattr(enrich, "lookup", lambda a, t: enrich.TrackInfo(album="From Deezer"))
    assert enrich.describe("A", "B")[1] == "deezer"

    monkeypatch.setattr(enrich, "lookup", lambda a, t: None)
    assert enrich.describe("A", "B") == (mb, "musicbrainz")


def test_musicbrainz_requests_are_spaced_and_identified(monkeypatch):
    import io
    import json as json_module

    clock = {"now": 100.0}
    sleeps, headers = [], []

    def urlopen(request, timeout):
        headers.append(request.headers)
        return io.BytesIO(json_module.dumps({"recordings": []}).encode())

    monkeypatch.setattr(enrich.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        enrich.time, "sleep", lambda s: sleeps.append(s) or clock.update(now=clock["now"] + s)
    )
    monkeypatch.setattr(enrich.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(enrich, "_mb_last", 0.0)

    enrich._mb_get("https://musicbrainz.org/ws/2/x")
    enrich._mb_get("https://musicbrainz.org/ws/2/y")

    assert sleeps == [pytest.approx(1.1)]  # MusicBrainz allows one request per second
    assert "local-Spotify" in headers[0]["User-agent"]
