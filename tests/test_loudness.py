"""ReplayGain: measured with ffmpeg, stored in tags, carried to the player."""

import importlib.util
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from adder import library, loudness

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"

SUMMARY = """
[Parsed_ebur128_0 @ 0x1] t: 0.9  TARGET:-23 LUFS    M: -22.0 S:-120.7     I: -21.9 LUFS
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.3 LUFS
    Threshold: -24.6 LUFS

  True peak:
    Peak:       -0.4 dBFS
"""


def test_the_summary_is_read_not_the_running_lines():
    gain, peak = loudness.parse_ebur128(SUMMARY)

    assert gain == pytest.approx(-3.7)  # -18 reference - (-14.3)
    assert peak == pytest.approx(10 ** (-0.4 / 20), abs=1e-6)


def test_silence_has_no_gain():
    assert loudness.parse_ebur128(SUMMARY.replace("-14.3 LUFS", "-inf LUFS")) is None
    assert loudness.parse_ebur128("no summary at all") is None


def test_the_fixture_is_measured_for_real():
    gain, peak = loudness.measure(FIXTURE)

    # The fixture is a -21.9 LUFS tone: 3.9 dB below the reference.
    assert gain == pytest.approx(3.9, abs=0.2)
    assert 0 < peak < 1


def test_missing_ffmpeg_is_not_an_error(monkeypatch):
    def gone(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(loudness.subprocess, "run", gone)

    assert loudness.measure(FIXTURE) is None


@pytest.mark.parametrize("suffix", [".m4a", ".mp3", ".flac", ".opus"])
def test_gain_round_trips_in_every_format(tmp_path, suffix):
    path = tmp_path / f"t{suffix}"
    if suffix == ".m4a":
        shutil.copy(FIXTURE, path)
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(FIXTURE), str(path)], check=True)

    loudness.write(path, -4.25, 0.98)

    assert library.read_tags(path)["gain"] == pytest.approx(-4.25)


@pytest.mark.parametrize(
    ("text", "value"),
    [("-4.20 dB", -4.2), ("+1.5 dB", 1.5), (b"3 dB", 3.0), ("", None), (None, None)],
)
def test_parse_gain(text, value):
    assert loudness.parse_gain(text) == value


# ---------------------------------------------------------------------------
# Nightly backfill
# ---------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location(
    "replaygain", Path(__file__).resolve().parents[1] / "scripts" / "replaygain.py"
)
replaygain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replaygain)


def test_backfill_keeps_the_arrival_date_and_the_analysis(tmp_path):
    root = tmp_path / "library"
    (root / "A").mkdir(parents=True)
    fresh, done = root / "A" / "fresh.m4a", root / "A" / "done.m4a"
    shutil.copy(FIXTURE, fresh)
    shutil.copy(FIXTURE, done)
    loudness.write(done, -1.0, 0.5)
    os.utime(fresh, (1_600_000_000, 1_600_000_000))

    db = tmp_path / "adder.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE audio_features(path TEXT PRIMARY KEY, sha256 TEXT)")
    con.execute(
        "INSERT INTO audio_features VALUES('A/fresh.m4a', ?)", (replaygain.file_hash(fresh),)
    )
    con.commit()
    con.close()

    assert replaygain.backfill(root, db) == (1, 1, 0)

    assert library.read_tags(fresh)["gain"] == pytest.approx(3.9, abs=0.2)
    assert fresh.stat().st_mtime == 1_600_000_000
    stored = sqlite3.connect(db).execute("SELECT sha256 FROM audio_features").fetchone()[0]
    assert stored == replaygain.file_hash(fresh)  # analysis will not redo it


def test_backfill_without_an_analysis_database(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    shutil.copy(FIXTURE, root / "t.m4a")

    assert replaygain.backfill(root, tmp_path / "missing.db") == (1, 0, 0)
