"""Pulling an edit made on the phone back into the .m3u.

The dangerous direction. Every test here exists because getting it wrong
rewrites a playlist of a thousand tracks with something shorter.
"""

import time
from datetime import datetime

import pytest

from adder import config as adder_config
from adder import covers, library, navidrome, playlists, runtime, sync


@pytest.fixture(autouse=True)
def forget_reconciled():
    """The seen-revisions cache is module state; tests must not inherit it."""
    sync._SEEN.clear()
    yield
    sync._SEEN.clear()


@pytest.fixture()
def temp_library(tmp_path, monkeypatch):
    monkeypatch.setattr(adder_config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    adder_config.LIBRARY.mkdir(parents=True)
    library.invalidate_library_index()
    return adder_config.LIBRARY


def _index(*paths):
    return [{"path": p, "artist": "A", "title": p, "duration": 100} for p in paths]


def _entry(name, path, count, ahead=600):
    """A Navidrome playlist row stamped ``ahead`` seconds after the file was written.

    The offset is taken from the machine rather than written in: stamping a
    local wall clock with somebody else's zone would shift every timestamp by
    hours and quietly invert what these tests assert.
    """
    when = datetime.fromtimestamp(time.time() + ahead).astimezone()
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789" + when.strftime("%z")
    stamp = stamp[:-2] + ":" + stamp[-2:]
    return {
        "id": "pl1",
        "name": name,
        "path": str(path),
        "sync": True,
        "songCount": count,
        "updatedAt": stamp,
    }


# ---------------------------------------------------------------------------
# Reading Navidrome's timestamps
# ---------------------------------------------------------------------------


def test_nanosecond_timestamps_are_readable():
    """Navidrome writes nine fractional digits; fromisoformat takes six.

    Getting None back here would make every playlist look "in step" and the
    whole feature would be silently inert.
    """
    assert sync._remote_epoch("2026-09-03T22:16:36.560289894+02:00") is not None
    assert sync._remote_epoch("2026-09-03T22:16:36.560289894Z") is not None
    assert sync._remote_epoch("") is None
    assert sync._remote_epoch("not a date") is None


# ---------------------------------------------------------------------------
# Deciding whether a playlist was edited elsewhere
# ---------------------------------------------------------------------------


def test_a_playlist_navidrome_just_ingested_is_not_treated_as_edited(temp_library, monkeypatch):
    """Our own write lands in Navidrome about six seconds later.

    If that counted as an edit, the loop would read our write back and fight
    itself on every single change.
    """
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])
    path = playlists.playlist_path("p")

    diverged, why = sync._diverged(_entry("p", path, 2, ahead=6))

    assert not diverged
    assert why == "in step with the file"


def test_an_edit_long_after_the_file_was_written_is_treated_as_remote(temp_library, monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])

    diverged, _ = sync._diverged(_entry("p", playlists.playlist_path("p"), 2))

    assert diverged


def test_playlists_without_a_file_are_left_alone(temp_library):
    """A playlist made in Amperfy has no .m3u and must not grow one."""
    entry = _entry("made-on-the-phone", "", 3)
    entry["sync"] = False

    assert sync._diverged(entry) == (False, "not backed by a file")


def test_a_playlist_whose_file_is_gone_is_left_alone(temp_library):
    diverged, why = sync._diverged(_entry("vanished", "/nowhere/vanished.m3u", 3))

    assert not diverged
    assert why == "no file on disk"


# ---------------------------------------------------------------------------
# Writing the remote order back
# ---------------------------------------------------------------------------


def test_a_remote_reorder_is_written_to_the_file(temp_library, monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a", "c.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a", "c.m4a"])
    monkeypatch.setattr(navidrome, "remote_tracks", lambda _id, _n: ["c.m4a", "a.m4a", "b.m4a"])

    result = sync.pull_back("p", _entry("p", playlists.playlist_path("p"), 3))

    assert result["changed"]
    assert [e.path for e in playlists.read("p").entries] == ["c.m4a", "a.m4a", "b.m4a"]


def test_an_order_that_already_matches_is_not_rewritten(temp_library, monkeypatch):
    """No write means no mtime change, so the loop cannot start oscillating."""
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])
    before = playlists.playlist_path("p").stat().st_mtime_ns
    monkeypatch.setattr(navidrome, "remote_tracks", lambda _id, _n: ["a.m4a", "b.m4a"])

    result = sync.pull_back("p", _entry("p", playlists.playlist_path("p"), 2))

    assert not result["changed"]
    assert playlists.playlist_path("p").stat().st_mtime_ns == before


def test_tracks_navidrome_cannot_see_survive_a_pull_back(temp_library, monkeypatch):
    """Navidrome drops an .m3u line whose file is missing, and never mentions it.

    Taking its list as the whole playlist would delete every track that
    happened to be missing at that moment -- which is exactly the set of tracks
    you would most want to notice was gone.
    """
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "gone.m4a", "b.m4a"])
    monkeypatch.setattr(navidrome, "remote_tracks", lambda _id, _n: ["b.m4a", "a.m4a"])

    result = sync.pull_back("p", _entry("p", playlists.playlist_path("p"), 2))

    assert result["kept_missing"] == 1
    assert [e.path for e in playlists.read("p").entries] == ["b.m4a", "a.m4a", "gone.m4a"]


def test_duplicate_entries_survive_a_pull_back(temp_library, monkeypatch):
    """Monday.m3u holds nineteen paths twice. A set would collapse them."""
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a", "a.m4a"])
    monkeypatch.setattr(navidrome, "remote_tracks", lambda _id, _n: ["a.m4a", "a.m4a", "b.m4a"])

    sync.pull_back("p", _entry("p", playlists.playlist_path("p"), 3))

    assert [e.path for e in playlists.read("p").entries] == ["a.m4a", "a.m4a", "b.m4a"]


def test_an_empty_remote_list_never_empties_a_playlist(temp_library, monkeypatch):
    """Navidrome down mid-request, or a playlist it has not read yet.

    Either way an empty answer must not be mistaken for "the phone deleted
    everything".
    """
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])
    monkeypatch.setattr(navidrome, "remote_tracks", lambda _id, _n: [])

    with pytest.raises(RuntimeError, match="empty playlist"):
        sync.pull_back("p", _entry("p", playlists.playlist_path("p"), 0))

    assert len(playlists.read("p").entries) == 2


def test_a_short_read_is_refused_rather_than_written(temp_library, monkeypatch):
    """remote_tracks itself guards this; the test pins the contract it relies on."""
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a", "c.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a", "c.m4a"])

    def truncated(_id, _n):
        raise navidrome.NavidromeUnavailable("returned 1 of 3")

    monkeypatch.setattr(navidrome, "remote_tracks", truncated)

    with pytest.raises(navidrome.NavidromeUnavailable):
        sync.pull_back("p", _entry("p", playlists.playlist_path("p"), 3))

    assert len(playlists.read("p").entries) == 3


# ---------------------------------------------------------------------------
# The pass as a whole
# ---------------------------------------------------------------------------


def test_a_failing_playlist_does_not_stop_the_others(temp_library, monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("bad", ["a.m4a", "b.m4a"])
    playlists.create("good", ["a.m4a", "b.m4a"])
    monkeypatch.setattr(navidrome, "configured", lambda: True)
    monkeypatch.setattr(
        navidrome,
        "playlists",
        lambda: [
            _entry("bad", playlists.playlist_path("bad"), 2),
            _entry("good", playlists.playlist_path("good"), 2),
        ],
    )

    def flaky(_id, _n):
        if flaky.calls == 0:
            flaky.calls += 1
            raise navidrome.NavidromeUnavailable("boom")
        return ["b.m4a", "a.m4a"]

    flaky.calls = 0
    monkeypatch.setattr(navidrome, "remote_tracks", flaky)

    report = sync.check(apply=True)

    by_name = {row["playlist"]: row for row in report["playlists"]}
    assert "error" in by_name["bad"]
    assert by_name["good"]["changed"]
    assert [e.path for e in playlists.read("good").entries] == ["b.m4a", "a.m4a"]


def test_a_dry_run_writes_nothing(temp_library, monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])
    before = playlists.playlist_path("p").read_bytes()
    monkeypatch.setattr(navidrome, "configured", lambda: True)
    monkeypatch.setattr(
        navidrome, "playlists", lambda: [_entry("p", playlists.playlist_path("p"), 2)]
    )
    monkeypatch.setattr(navidrome, "remote_tracks", lambda _id, _n: ["b.m4a", "a.m4a"])

    report = sync.check(apply=False)

    assert report["playlists"][0]["changed"] is True
    assert playlists.playlist_path("p").read_bytes() == before


def test_a_revision_already_reconciled_is_not_read_again(temp_library, monkeypatch):
    """Navidrome moves updatedAt on its own.

    Monday sat five days "ahead" of a file nobody had touched, so every pass
    re-read all 1075 of its tracks to conclude nothing had changed. Once a
    revision has been looked at, it is not looked at again until it moves.
    """
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])
    entry = _entry("p", playlists.playlist_path("p"), 2)
    monkeypatch.setattr(navidrome, "configured", lambda: True)
    monkeypatch.setattr(navidrome, "playlists", lambda: [entry])

    reads = []

    def counting(playlist_id, count):
        reads.append(playlist_id)
        return ["a.m4a", "b.m4a"]

    monkeypatch.setattr(navidrome, "remote_tracks", counting)

    sync.check(apply=True)
    sync.check(apply=True)
    sync.check(apply=True)
    assert len(reads) == 1

    # A new revision is a new question.
    entry["updatedAt"] = entry["updatedAt"].replace(".123456789", ".987654321")
    sync.check(apply=True)
    assert len(reads) == 2


def test_a_playlist_that_could_not_be_read_is_tried_again(temp_library, monkeypatch):
    """A failure is not a reconciliation. Recording it would hide the playlist
    until Navidrome happened to touch it again."""
    monkeypatch.setattr(library, "library_index", lambda: _index("a.m4a", "b.m4a"))
    playlists.create("p", ["a.m4a", "b.m4a"])
    monkeypatch.setattr(navidrome, "configured", lambda: True)
    monkeypatch.setattr(
        navidrome, "playlists", lambda: [_entry("p", playlists.playlist_path("p"), 2)]
    )

    attempts = []

    def failing(playlist_id, count):
        attempts.append(playlist_id)
        raise navidrome.NavidromeUnavailable("down")

    monkeypatch.setattr(navidrome, "remote_tracks", failing)

    sync.check(apply=True)
    sync.check(apply=True)

    assert len(attempts) == 2


def test_check_without_navidrome_is_quiet(monkeypatch):
    monkeypatch.setattr(navidrome, "configured", lambda: False)

    assert sync.check()["navidrome"] == "not configured"
