"""scripts/upgrade_audio.py: finding a track's link and swapping its audio in.

YouTube is never reached: the swap is tested on files made from the fixture,
and the parts that talk to YouTube are replaced.
"""

import importlib.util
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4, MP4FreeForm

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"

spec = importlib.util.spec_from_file_location(
    "upgrade_audio", Path(__file__).resolve().parents[1] / "scripts" / "upgrade_audio.py"
)
assert spec and spec.loader
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "DATA", tmp_path / "data")
    monkeypatch.setattr(upgrade, "BACKUP", tmp_path / "data" / "backup")
    monkeypatch.setattr(upgrade, "STATE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(upgrade, "WORK", tmp_path / "data" / "work")
    root = tmp_path / "library"
    (root / "Artist" / "Singles").mkdir(parents=True)
    return root


def _library_track(root):
    """An AAC track as the library holds it: tags, a cover, an old date."""
    path = root / "Artist" / "Singles" / "Song.m4a"
    path.write_bytes(FIXTURE.read_bytes())
    audio = MP4(path)
    audio["\xa9nam"] = ["Song"]
    audio["\xa9ART"] = ["Artist"]
    audio["\xa9alb"] = ["Album"]
    audio["trkn"] = [(3, 9)]
    audio["----:com.apple.iTunes:replaygain_track_gain"] = [MP4FreeForm(b"-1.00 dB")]
    audio.save()
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return path


def _opus_download(tmp_path):
    source = tmp_path / "vid.opus"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(FIXTURE), "-c:a", "libopus", str(source)], check=True
    )
    from adder import ingest

    return ingest.ensure_m4a(source)


def _codec(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def test_youtube_ids_are_read_from_every_link_form():
    assert upgrade.youtube_id("https://www.youtube.com/watch?v=abc123&t=4") == "abc123"
    assert upgrade.youtube_id("https://youtu.be/abc123") == "abc123"
    assert upgrade.youtube_id("https://music.youtube.com/watch?v=abc123") == "abc123"
    assert upgrade.youtube_id("file:3917bf57") is None
    assert upgrade.youtube_id("") is None


def test_known_links_come_first_and_are_not_repeated():
    tags = {"source": "https://youtu.be/one", "artist": "A • B", "title": "Song"}
    links = {"A/Singles/Song.m4a": "https://www.youtube.com/watch?v=two"}
    by_name = {("a", "song"): "https://youtu.be/one"}

    got = upgrade.candidates_for("A/Singles/Song.m4a", tags, links, by_name)

    assert got == ["https://youtu.be/one", "https://www.youtube.com/watch?v=two"]


def test_uploads_are_told_by_name_so_a_moved_one_is_still_kept(tmp_path):
    db = tmp_path / "adder.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE tasks(url TEXT, status TEXT, artist TEXT, title TEXT, result_path TEXT)"
        )
        con.executemany(
            "INSERT INTO tasks VALUES (?, 'done', ?, ?, ?)",
            [
                ("https://www.youtube.com/watch?v=x1", "A", "s", "A/Singles/s.m4a"),
                ("file:abc", "BUSHIDO ZHO", "PAPI", "BUSHIDO ZHO/Singles/PAPI.m4a"),
            ],
        )

    links, uploads = upgrade.known_links(db)

    assert links == {"A/Singles/s.m4a": "https://www.youtube.com/watch?v=x1"}
    # Moved to another artist folder from the panel: the task row kept the old path.
    moved = {"artist": "BUSHIDO ZHO \u2022 Yanix", "title": "PAPI"}
    assert upgrade.is_upload(moved, uploads)
    assert not upgrade.is_upload({"artist": "A", "title": "s"}, uploads)


def test_titles_that_name_another_version_are_refused():
    assert upgrade.names_another_version("Song (Clean)", "Song")
    assert upgrade.names_another_version("Song [sped up]", "Song")
    assert upgrade.names_another_version("Песня (ускоренная)", "Песня")
    assert not upgrade.names_another_version("Song (Official Video)", "Song")
    assert not upgrade.names_another_version("Song (Remix)", "Song (Remix)")
    assert not upgrade.names_another_version("Olivia - Song", "Song")  # "live" as a word only


def test_only_a_refusal_from_youtube_stops_the_run():
    upgrade._judge(RuntimeError("ERROR: Sign in to confirm your age"))  # this video only
    upgrade._judge(RuntimeError("ERROR: Video unavailable"))
    with pytest.raises(upgrade.Blocked):
        upgrade._judge(RuntimeError("ERROR: Sign in to confirm you're not a bot"))
    with pytest.raises(upgrade.Blocked):
        upgrade._judge(RuntimeError("ERROR: This content isn't available, try again later"))
    with pytest.raises(upgrade.Blocked):
        upgrade._judge(RuntimeError("ERROR: HTTP Error 429: Too Many Requests"))


def test_a_refusal_while_reading_search_results_is_not_swallowed():
    # The results are lazy: YouTube is asked while they are read.
    def entries():
        raise RuntimeError("ERROR: HTTP Error 429: Too Many Requests")
        yield

    class Client:
        def extract_info(self, *a, **k):
            return {"entries": entries()}

    with pytest.raises(upgrade.Blocked):
        upgrade.search(Client(), "A", "Song", 100)


def test_the_swap_keeps_tags_date_and_a_backup(dirs, tmp_path):
    old = _library_track(dirs)
    before = old.read_bytes()
    new = _opus_download(tmp_path)

    upgrade.swap(old, new, "https://www.youtube.com/watch?v=vid", dirs, tmp_path / "none.db")

    assert _codec(old) == "opus"
    tags = MP4(old).tags
    assert tags["\xa9nam"] == ["Song"] and tags["\xa9alb"] == ["Album"]
    assert tags["trkn"] == [(3, 9)]
    assert bytes(tags[upgrade.MP4_SOURCE][0]) == b"https://www.youtube.com/watch?v=vid"
    # ReplayGain is measured again on the new audio, not copied.
    assert bytes(tags["----:com.apple.iTunes:replaygain_track_gain"][0]) != b"-1.00 dB"
    assert old.stat().st_mtime == 1_700_000_000
    assert (upgrade.BACKUP / "Artist" / "Singles" / "Song.m4a").read_bytes() == before
    assert not new.exists()
    assert [p.name for p in old.parent.iterdir()] == ["Song.m4a"]


def test_the_analysis_row_follows_the_new_file(dirs, tmp_path):
    from adder import loudness

    old = _library_track(dirs)
    db = tmp_path / "adder.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE audio_features(path TEXT, sha256 TEXT, tempo REAL)")
        con.execute(
            "INSERT INTO audio_features VALUES (?, ?, 120)",
            ("Artist/Singles/Song.m4a", loudness.file_hash(old)),
        )

    upgrade.swap(old, _opus_download(tmp_path), "https://youtu.be/vid", dirs, db)

    with sqlite3.connect(db) as con:
        (sha,) = con.execute("SELECT sha256 FROM audio_features").fetchone()
    assert sha == loudness.file_hash(old)


def test_a_file_edited_during_the_swap_is_left_alone(dirs, tmp_path, monkeypatch):
    old = _library_track(dirs)
    new = _opus_download(tmp_path)
    real_apply = upgrade.loudness.apply

    def apply_and_edit(path):
        # The panel saves a tag while ReplayGain is being measured, and puts
        # the mtime back as it always does: only ctime tells.
        edited = MP4(old)
        edited["\xa9alb"] = ["Other"]
        edited.save()
        os.utime(old, (1_700_000_000, 1_700_000_000))
        return real_apply(path)

    monkeypatch.setattr(upgrade.loudness, "apply", apply_and_edit)

    with pytest.raises(RuntimeError):
        upgrade.swap(old, new, "https://youtu.be/vid", dirs, tmp_path / "none.db")

    assert _codec(old) == "aac"
    assert MP4(old).tags["\xa9alb"] == ["Other"]


def test_the_same_recording_matches_and_others_do_not(tmp_path):
    pytest.importorskip("numpy")
    base = tmp_path / "base.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=40,volume=0.5",
         "-f", "lavfi", "-i", "anoisesrc=d=40:a=0.2:seed=1", "-filter_complex", "amix=inputs=2",
         "-c:a", "aac", "-b:a", "256k", "-f", "mp4", str(base)],
        check=True,
    )  # fmt: skip

    def variant(name, *args):
        out = tmp_path / f"{name}.opus"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(base), *args, "-c:a", "libopus", str(out)],
            check=True,
        )
        return out

    same = variant("same")
    bleeped = variant("bleeped", "-af", "volume=enable='between(t,20,20.4)':volume=0")
    other = tmp_path / "other.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=40:a=0.3:seed=2",
         "-c:a", "aac", "-f", "mp4", str(other)],
        check=True,
    )  # fmt: skip

    overall, worst, _ = upgrade.similarity(base, same)
    assert overall >= upgrade.MIN_CORRELATION and worst >= upgrade.MIN_WINDOW_CORRELATION
    # A bleep barely moves the whole-track figure but empties its second.
    overall, worst, _ = upgrade.similarity(base, bleeped)
    assert overall >= upgrade.MIN_CORRELATION
    assert worst < upgrade.MIN_WINDOW_CORRELATION
    overall, _, _ = upgrade.similarity(base, other)
    assert overall < upgrade.MIN_CORRELATION
