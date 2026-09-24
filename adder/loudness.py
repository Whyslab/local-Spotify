"""ReplayGain: every track measured once, played back at the same loudness.

Tracks from YouTube differ a lot in how loud they were mastered, and a shuffle
jumps between them. The fix players have agreed on is ReplayGain: measure each
track's integrated loudness, store how far it is from a reference level in a
tag, and let the player turn it down (or up) by that much. Navidrome and most
Subsonic clients read the tag; this project's own web player applies it too.

Measured with ffmpeg's ebur128 filter (EBU R 128, the same measurement
ReplayGain 2.0 is defined on) against the ReplayGain 2.0 reference of -18 LUFS.
ffmpeg is already a hard dependency, so nothing new is installed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import mutagen

logger = logging.getLogger(__name__)

REFERENCE_LUFS = -18.0
GAIN_TAG = "REPLAYGAIN_TRACK_GAIN"
PEAK_TAG = "REPLAYGAIN_TRACK_PEAK"
MP4_PREFIX = "----:com.apple.iTunes:"


def measure(path: Path, timeout: float = 300) -> tuple[float, float] | None:
    """(gain in dB, true peak as a linear ratio), or None when it cannot be measured."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                "-af", "ebur128=peak=true", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Loudness of %s not measured: %s", path.name, exc, extra={"task_id": "system"}
        )
        return None
    return parse_ebur128(result.stderr)


def parse_ebur128(output: str) -> tuple[float, float] | None:
    """Read the summary ebur128 prints at the end of its run."""
    # The summary comes last; the per-frame lines before it carry "I:" too.
    summary = output[output.rfind("Summary:") :] if "Summary:" in output else ""
    integrated = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summary)
    if not integrated or integrated.group(1) == "-inf":
        return None  # silence has no loudness to correct
    gain = REFERENCE_LUFS - float(integrated.group(1))
    peak_db = float(peak.group(1)) if peak and peak.group(1) != "-inf" else 0.0
    return round(gain, 2), round(10 ** (peak_db / 20), 6)


def write(path: Path, gain: float, peak: float) -> None:
    """Store the result in the tags every ReplayGain-aware player looks for."""
    gain_text, peak_text = f"{gain:.2f} dB", f"{peak:.6f}"
    suffix = path.suffix.lower()
    if suffix == ".m4a":
        from mutagen.mp4 import MP4, MP4FreeForm

        audio = MP4(path)
        audio[MP4_PREFIX + GAIN_TAG.lower()] = [MP4FreeForm(gain_text.encode())]
        audio[MP4_PREFIX + PEAK_TAG.lower()] = [MP4FreeForm(peak_text.encode())]
        audio.save()
    elif suffix == ".mp3":
        from mutagen.id3 import TXXX
        from mutagen.mp3 import MP3

        mp3 = MP3(path)
        if mp3.tags is None:
            mp3.add_tags()
        assert mp3.tags is not None
        for tag, text in ((GAIN_TAG, gain_text), (PEAK_TAG, peak_text)):
            mp3.tags.setall(f"TXXX:{tag}", [TXXX(encoding=3, desc=tag, text=[text])])
        mp3.save()
    else:
        other = mutagen.File(path)
        if other is None:
            return
        other[GAIN_TAG.lower()] = [gain_text]
        other[PEAK_TAG.lower()] = [peak_text]
        other.save()


def parse_gain(text: str | bytes | None) -> float | None:
    """'-4.20 dB' -> -4.2; anything unreadable -> None."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    match = re.match(r"\s*([-+]?\d+(?:\.\d+)?)", text or "")
    return float(match.group(1)) if match else None


def apply(path: Path) -> float | None:
    """Measure and tag one file. Returns the gain, or None if it was not measured."""
    result = measure(path)
    if result is None:
        return None
    gain, peak = result
    write(path, gain, peak)
    return gain


# ---------------------------------------------------------------------------
# Catching up on tracks that arrived before ReplayGain did
# ---------------------------------------------------------------------------
#
# Two things a retag must not disturb. The file's modification time: the
# library reads it as "when this track arrived", so rewriting a thousand files
# would make all of them new. And the audio analysis: scripts/analyze_audio.py
# keys every measured track by the SHA-256 of the whole file, so a new tag
# would make the next pass re-measure an hour of unchanged audio. Rows whose
# hash matched the file before tagging are moved to the new hash.


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _analysis(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path, timeout=30)
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audio_features'"
    ).fetchone()
    if not exists:
        con.close()
        return None
    return con


def backfill(root: Path, db_path: Path, limit: int | None = None) -> tuple[int, int, int]:
    """Tag what is missing. Returns (measured, already tagged, failed)."""
    from . import library

    root = root.resolve()
    analysis = _analysis(db_path)
    measured = tagged = failed = 0
    files = sorted(
        p for suffix in library.AUDIO_SUFFIXES for p in root.rglob(f"*{suffix}") if p.is_file()
    )
    try:
        for path in files:
            if limit is not None and measured >= limit:
                break
            try:
                if library.read_tags(path).get("gain") is not None:
                    tagged += 1
                    continue
            except Exception:
                failed += 1
                continue

            stat = path.stat()
            before = file_hash(path) if analysis else None
            try:
                gain = apply(path)
            except Exception as exc:
                logger.warning("%s: %s", path.relative_to(root), exc)
                gain = None
            finally:
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if gain is None:
                failed += 1
                continue
            measured += 1
            if analysis is not None:
                analysis.execute(
                    "UPDATE audio_features SET sha256 = ? WHERE path = ? AND sha256 = ?",
                    (file_hash(path), str(path.relative_to(root)), before),
                )
                analysis.commit()
            logger.info("%+.2f dB  %s", gain, path.relative_to(root))
    finally:
        if analysis is not None:
            analysis.close()
    library.invalidate_library_index()
    return measured, tagged, failed
