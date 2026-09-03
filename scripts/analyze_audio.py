#!/usr/bin/env python3
"""Measure tempo, energy and key for every track, so shuffling can be about something.

The library has 1109 tracks and not one genre or BPM tag between them -- there
is nothing to sort by except the audio itself. This reads it.

Run it as a bounded job rather than from the service. On a two-core laptop with
under two gigabytes free, an unbounded analysis is how earlyoom ends up killing
the browser instead:

    systemd-run --user --scope -p MemoryMax=1500M \\
        nice -n 10 .venv/bin/python scripts/analyze_audio.py

It resumes. Every track is keyed by its path and content hash, so a run that is
interrupted -- or killed -- picks up where it stopped, and a track that has not
changed is never measured twice.
"""

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Only the middle of a track is analysed. Ninety seconds is more than enough to
# establish tempo and key, it bounds memory to a few megabytes whatever the
# track length, and it skips the intro and the outro -- which are the least
# representative parts of a song for both.
ANALYSIS_SECONDS = 90
SAMPLE_RATE = 22050

# Krumhansl-Schmuckler profiles: how strongly each scale degree is weighted in
# a major and a minor key. Correlating a track's chroma against all 24
# rotations is the standard way of naming a key without a score.
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS audio_features(
        path TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        tempo REAL, energy REAL, brightness REAL,
        music_key TEXT, mode TEXT,
        analyzed_at TEXT DEFAULT (datetime('now','localtime')))""")
    con.commit()
    return con


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def estimate_key(chroma) -> tuple[str, str]:
    """Name the key by correlating the chroma against all 24 rotations."""
    import numpy as np

    profile = chroma.mean(axis=1)
    if not profile.any():
        return "", ""
    profile = profile / profile.sum()

    best = (-2.0, "", "")
    for index in range(12):
        for name, template in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = np.roll(np.array(template, dtype=float), index)
            correlation = float(np.corrcoef(profile, rotated)[0, 1])
            if correlation > best[0]:
                best = (correlation, NOTE_NAMES[index], name)
    return best[1], best[2]


def decode(path: Path) -> "np.ndarray":  # noqa: F821
    """Decode the middle of a track to mono float32 with ffmpeg.

    Not librosa's own loader: that reads through libsndfile, which does not
    handle AAC, and almost every track here is .m4a. ffmpeg reads all four
    formats the library accepts and is already required by the project, so
    this is one path instead of two and one dependency fewer.
    """
    import numpy as np

    total = probe_duration(path)
    offset = max(0.0, (total - ANALYSIS_SECONDS) / 2)

    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{offset:.3f}",
            "-t",
            str(ANALYSIS_SECONDS),
            "-i",
            str(path),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-120:])
    return np.frombuffer(result.stdout, dtype=np.float32)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def analyse(path: Path) -> dict:
    import librosa

    # librosa loads submodules lazily, and rhythm is not reachable through the
    # top-level package until it is imported by name.
    import librosa.feature.rhythm
    import numpy as np

    samples = decode(path)
    rate = SAMPLE_RATE
    if samples.size == 0:
        raise ValueError("no audio decoded")

    tempo = float(np.atleast_1d(librosa.feature.rhythm.tempo(y=samples, sr=rate))[0])
    # Loudness in the plain sense: how hard the track is pushing on average.
    energy = float(np.mean(librosa.feature.rms(y=samples)))
    # Where the weight of the spectrum sits. Two tracks at the same tempo and
    # loudness still feel different if one is all bass and the other all
    # cymbals, and this is the cheap way to tell them apart.
    brightness = float(np.mean(librosa.feature.spectral_centroid(y=samples, sr=rate)))
    music_key, mode = estimate_key(librosa.feature.chroma_cqt(y=samples, sr=rate))

    return {
        "tempo": round(tempo, 2),
        "energy": round(energy, 6),
        "brightness": round(brightness, 2),
        "music_key": music_key,
        "mode": mode,
    }


def _measure_one(job):
    """Worker body: analyse one file and report, never raise across the pool."""
    path, rel, digest = job
    try:
        features = analyse(path)
    except Exception as exc:  # noqa: BLE001
        return rel, digest, None, str(exc)
    # Hashing after the decode rather than before it: a file that cannot be
    # analysed does not need a hash, and this way the cost falls on the tracks
    # that are actually being measured.
    return rel, digest or file_hash(path), features, None


def measure_all(jobs, workers: int):
    """Yield results in completion order, one process or several.

    Only the parent writes to SQLite. Several analysing processes could share
    the database -- it is in WAL -- but keeping the writes in one place means
    the resume point can never disagree with itself.
    """
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            yield _measure_one(job)
        return

    import multiprocessing

    with multiprocessing.Pool(processes=workers) as pool:
        yield from pool.imap_unordered(_measure_one, jobs, chunksize=1)


def library_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    # is_file() is not decoration: one of the artists in this library is called
    # "nyan.mp3", so a glob for *.mp3 matches a directory.
    return sorted(p for suffix in suffixes for p in root.rglob(f"*{suffix}") if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO / "adder" / "adder.db")
    parser.add_argument("--limit", type=int, default=0, help="stop after N tracks")
    parser.add_argument("--only", type=Path, default=None, help="analyse one file and stop")
    parser.add_argument("--force", action="store_true", help="re-measure even if unchanged")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help=(
            "how many tracks to measure at once. One by default, because the "
            "machine is usually in use; a process peaks at about 376 MB, so "
            "three fit inside MemoryMax=1500M for an overnight run."
        ),
    )
    args = parser.parse_args()

    os.environ.setdefault("API_TOKEN", "analysis")
    from adder import config, library

    root = (args.library or config.LIBRARY).resolve()
    con = connect(args.db)

    targets = [args.only.resolve()] if args.only else library_files(root, library.AUDIO_SUFFIXES)

    known = {
        row[0]: row[1] for row in con.execute("SELECT path, sha256 FROM audio_features").fetchall()
    }

    pending = []
    skipped = 0
    for path in targets:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        # Hash only what there is something to compare against. A track with no
        # record needs measuring whatever its hash is, and hashing the whole
        # library first meant reading nine gigabytes before the first track was
        # analysed -- and reading all of it again for a run with --limit.
        previous = known.get(rel)
        if previous is not None and not args.force:
            if file_hash(path) == previous:
                skipped += 1
                continue
            digest = file_hash(path)
        else:
            digest = None  # computed after the analysis succeeds

        pending.append((path, rel, digest))
        if args.limit and len(pending) >= args.limit:
            break

    done = failed = 0
    started = time.monotonic()

    for rel, digest, features, error in measure_all(pending, args.jobs):
        if error is not None:
            failed += 1
            print(f"  ✗ {rel}: {error[:90]}", flush=True)
            continue

        con.execute(
            "INSERT INTO audio_features(path, sha256, tempo, energy, brightness, music_key, mode, "
            "analyzed_at) VALUES(?,?,?,?,?,?,?, datetime('now','localtime')) "
            "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, tempo=excluded.tempo, "
            "energy=excluded.energy, brightness=excluded.brightness, "
            "music_key=excluded.music_key, mode=excluded.mode, analyzed_at=excluded.analyzed_at",
            (
                rel,
                digest,
                features["tempo"],
                features["energy"],
                features["brightness"],
                features["music_key"],
                features["mode"],
            ),
        )
        # Committed per track on purpose: this is what makes the run resumable
        # when it is killed rather than finished.
        con.commit()
        done += 1

        if done % 10 == 0:
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed else 0
            remaining = len(pending) - done - failed
            eta = remaining / rate if rate else 0
            print(
                f"  {done} готово, {skipped} без изменений, {failed} с ошибкой, "
                f"осталось ~{remaining} (~{eta / 60:.0f} мин)",
                flush=True,
            )

    print(f"Итог: измерено {done}, пропущено {skipped}, с ошибкой {failed}", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
