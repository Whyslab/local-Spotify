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

import logging
import re
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

        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        for tag, text in ((GAIN_TAG, gain_text), (PEAK_TAG, peak_text)):
            audio.tags.setall(f"TXXX:{tag}", [TXXX(encoding=3, desc=tag, text=[text])])
        audio.save()
    else:
        audio = mutagen.File(path)
        if audio is None:
            return
        audio[GAIN_TAG.lower()] = [gain_text]
        audio[PEAK_TAG.lower()] = [peak_text]
        audio.save()


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
