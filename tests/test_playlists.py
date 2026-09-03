"""Playlist files: identity by line, atomic writes, and refusing stale edits."""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from adder import config, covers, library, playlists, runtime


@pytest.fixture()
def temp_library(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    config.LIBRARY.mkdir(parents=True)
    # The index is a module-level cache; a stale one would leak between tests.
    library.invalidate_library_index()
    monkeypatch.setattr(library, "library_index", lambda: [])
    return config.LIBRARY


def test_duplicate_paths_keep_their_own_positions(temp_library):
    """Monday.m3u has nineteen paths that appear twice; a path is not an identity.

    If ordering went by path, the two copies would be indistinguishable and a
    drag would move whichever one happened to be found first.
    """
    same = "Artist/Singles/Song.m4a"
    playlists.create("dup", [same, "Other/Singles/B.m4a", same])

    playlist = playlists.read("dup")

    assert [e.index for e in playlist.entries] == [0, 1, 2]
    assert [e.path for e in playlist.entries] == [same, "Other/Singles/B.m4a", same]

    # Moving the *second* copy to the front must leave the first one alone.
    reordered = [same, same, "Other/Singles/B.m4a"]
    playlists.write("dup", reordered, playlist.revision)

    assert [e.path for e in playlists.read("dup").entries] == reordered


def test_write_refuses_an_edit_made_against_a_stale_view(temp_library):
    """Two devices, one file: the second writer must be told, not silently win."""
    playlists.create("shared", ["A.m4a", "B.m4a"])
    stale = playlists.read("shared").revision

    playlists.write("shared", ["B.m4a", "A.m4a"], stale)  # the phone edits first

    with pytest.raises(HTTPException) as excinfo:  # the laptop still holds the old view
        playlists.write("shared", ["A.m4a", "A.m4a"], stale)

    assert excinfo.value.status_code == 409
    assert [e.path for e in playlists.read("shared").entries] == ["B.m4a", "A.m4a"]


def test_write_without_a_revision_is_allowed(temp_library):
    """Callers that genuinely want to overwrite can, deliberately."""
    playlists.create("forced", ["A.m4a"])
    playlists.write("forced", ["B.m4a"], None)
    assert [e.path for e in playlists.read("forced").entries] == ["B.m4a"]


def test_a_failed_write_leaves_the_previous_file_intact(temp_library, monkeypatch):
    """The nightmare is an empty .m3u: Navidrome mirrors it within ten seconds."""
    playlists.create("precious", ["A.m4a", "B.m4a", "C.m4a"])
    before = playlists.playlist_path("precious").read_text(encoding="utf-8")

    real_replace = os.replace

    def explode(src, dst):
        raise OSError("disk went away mid-write")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        playlists.write("precious", ["D.m4a"], None)
    monkeypatch.setattr(os, "replace", real_replace)

    assert playlists.playlist_path("precious").read_text(encoding="utf-8") == before
    # And no half-written temporary file is left lying around in the library.
    assert not list(temp_library.glob(".*tmp*"))


def test_previous_versions_are_kept(temp_library):
    playlists.create("versioned", ["A.m4a"])
    for track in ("B.m4a", "C.m4a", "D.m4a"):
        playlists.write("versioned", [track], None)

    history = sorted((playlists.HISTORY_DIR / "versioned").glob("*.m3u"))
    assert len(history) == 3  # one per overwrite
    assert "A.m4a" in history[0].read_text(encoding="utf-8")


def test_history_is_capped(temp_library, monkeypatch):
    monkeypatch.setattr(playlists, "HISTORY_KEEP", 3)
    playlists.create("capped", ["A.m4a"])
    for i in range(6):
        playlists.write("capped", [f"{i}.m4a"], None)

    assert len(list((playlists.HISTORY_DIR / "capped").glob("*.m3u"))) == 3


@pytest.mark.parametrize(
    "name",
    ["../escape", "sub/dir", "..", ".", ".hidden", "", "   ", "nul\x00byte"],
)
def test_illegal_playlist_names_are_refused(temp_library, name):
    """The name comes from a URL path and becomes a file name, on a LAN service."""
    with pytest.raises(HTTPException) as excinfo:
        playlists.playlist_path(name)
    assert excinfo.value.status_code == 400


def test_names_with_cyrillic_emoji_and_commas_round_trip(temp_library):
    name = "Ночь, зал 🎧"
    tracks = ["Артист, и другой/Singles/Трек, часть 2.m4a", "PHARAOH/Singles/Бентли.m4a"]
    playlists.create(name, tracks)

    assert [e.path for e in playlists.read(name).entries] == tracks
    assert playlists.playlist_path(name).read_text(encoding="utf-8").startswith("#EXTM3U")


def test_rename_moves_the_file(temp_library):
    playlists.create("old", ["A.m4a"])
    playlists.rename("old", "new")

    assert not playlists.playlist_path("old").exists()
    assert [e.path for e in playlists.read("new").entries] == ["A.m4a"]


def test_delete_moves_the_playlist_to_trash(temp_library):
    playlists.create("doomed", ["A.m4a"])
    result = playlists.delete("doomed")

    assert not playlists.playlist_path("doomed").exists()
    assert Path(result["trash"]).exists()


def test_unknown_paths_survive_a_rewrite(temp_library):
    """A track that is missing today should not vanish from the playlist forever."""
    playlists.create("gaps", ["Present.m4a", "Missing/For/Now.m4a"])
    playlist = playlists.read("gaps")

    playlists.write("gaps", [e.path for e in playlist.entries], playlist.revision)

    assert [e.path for e in playlists.read("gaps").entries] == [
        "Present.m4a",
        "Missing/For/Now.m4a",
    ]


def test_extinf_lines_are_written(temp_library, monkeypatch):
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [
            {
                "path": "Artist/Singles/Song.m4a",
                "artist": "Artist",
                "title": "Song",
                "album": "",
                "duration": 123.4,
                "albumartist": "",
                "track": None,
                "haystack": "",
            }
        ],
    )
    playlists.create("tagged", ["Artist/Singles/Song.m4a"])

    text = playlists.playlist_path("tagged").read_text(encoding="utf-8")
    assert "#EXTINF:123,Artist - Song" in text


def test_listing_reports_counts_and_revisions(temp_library):
    playlists.create("one", ["A.m4a"])
    playlists.create("two", ["A.m4a", "B.m4a"])

    listing = {entry["name"]: entry for entry in playlists.listing()}

    assert listing["one"]["tracks"] == 1
    assert listing["two"]["tracks"] == 2
    assert listing["one"]["revision"] != listing["two"]["revision"]
