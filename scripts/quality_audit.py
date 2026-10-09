#!/usr/bin/env python3
"""Check the library's sound quality, read only.

For every track: codec, bitrate, sample rate and length (ffprobe); how much
energy is left in 16.5-19 kHz and in 19.5-21 kHz, as total power against the
1-16 kHz band (a Welch spectrum over three 10-second stretches, at 20, 50 and
80 % of the track; a flat spectrum reads -7.8 and -10 dB, since the reference
band is wider); and the peaks: how high, how many samples over full scale, how
many in flat tops (the whole track decoded).

What the numbers mean:

* above 16 kHz, music sits some 15-45 dB under the 1-16 kHz band (measured on
  30 tracks of this library, 2026-10-09). A lossy encode cut at 16 kHz --
  YouTube's AAC stream, or an upload made from one -- leaves 60 dB and more
  less: ``cut16``.
* ``clipped``: CLIP_SAMPLES or more samples in flat tops -- FLAT_RUN or more
  equal samples in a row at the channel's own peak (within PEAK_TOLERANCE;
  counted where such a run starts, so a run of n samples counts n - 7), so
  a master clipped and then turned down a little counts too. That is what
  hard clipping leaves in PCM (and in lossless files). A clean low sine at the
  peak of a 16-bit file stays within one step for only a few samples (about 3
  at 40 Hz), which is why a run must be FLAT_RUN long. A lossy decode of a
  loud master goes over full scale without any flat top: almost every track
  from YouTube does (peaks up to 1.4, thousands of samples over 1.0, measured
  2026-10-09), and the player only ever turns it down (ReplayGain), so that
  count -- ``over_full_scale`` -- is reported, not flagged.
* ``low_bitrate`` / ``low_rate``: under LOW_BITRATE for the codec, or under
  44.1 kHz.

Each flagged track gets advice: an AAC file -> ``upgrade_audio.py`` (YouTube's
Opus stream is usually better, and that script proves it is the same
recording before it swaps; it leaves uploads alone); anything else cut at 16 kHz -> ``other source``
(the upload itself is the problem). Nothing is changed here: replacing files
is the user's decision.

Usage (the decode is heavy; keep it out of the music's way):

    systemd-run --user --unit=quality-audit -p WorkingDirectory=$PWD \\
        -p MemoryMax=1500M -p Nice=10 \\
        $PWD/.venv/bin/python scripts/quality_audit.py --json $HOME/quality.jsonl

The report is written as it goes, one JSON line per track, so a stopped run
keeps what it did.

Needs ffmpeg/ffprobe, numpy and scipy (scripts/requirements-analysis.txt).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from adder.config import LIBRARY
except Exception:  # no API_TOKEN / no .env: the default layout still works
    LIBRARY = Path.home() / "Music" / "Normalized Library"

AUDIO_SUFFIXES = (".m4a", ".mp3", ".flac", ".opus", ".ogg")  # as adder/library.py
SR = 48000
SEGMENTS = (0.2, 0.5, 0.8)
SEGMENT_SECONDS = 10.0
CUT_DB = -60.0  # above 16 kHz, relative to 1-16 kHz
FLAT_RUN = 8  # equal samples in a row
PEAK_TOLERANCE = 2 / 32768  # two 16-bit steps under the channel's peak
CLIP_FLOOR = 0.5  # a quiet track's loudest run is not clipping
CLIP_SAMPLES = 1000
LOW_BITRATE = {"aac": 128, "opus": 96, "mp3": 192, "vorbis": 128}  # kbit/s
STREAM_FIELDS = "codec_name,sample_rate,channels,bit_rate"
CHUNK = 1 << 20  # bytes of float32 read at a time when counting full-scale samples


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", f"stream={STREAM_FIELDS}:format=bit_rate,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    bitrate = stream.get("bit_rate") or fmt.get("bit_rate")
    return {
        "codec": stream.get("codec_name", ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "bitrate": round(int(bitrate) / 1000) if bitrate else None,
        "duration": round(float(fmt.get("duration") or 0), 2),
    }


def _decode(path: Path, start: float, seconds: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", str(path),
         "-map", "0:a:0", "-t", f"{seconds:.3f}", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32)


def spectrum(path: Path, duration: float) -> tuple[float | None, float | None]:
    """Power in 16.5-19 and 19.5-21 kHz, dB against the 1-16 kHz band."""
    from scipy.signal import welch

    spectra = []
    freqs = np.zeros(0)
    for at in SEGMENTS:
        start = max(0.0, duration * at - SEGMENT_SECONDS / 2)
        x = _decode(path, start, SEGMENT_SECONDS)
        if len(x) >= 8192:
            freqs, power = welch(x, SR, nperseg=8192)
            spectra.append(power)
    if not spectra:
        return None, None
    power = np.mean(spectra, axis=0)

    def band(lo: float, hi: float) -> float:
        return float(power[(freqs >= lo) & (freqs < hi)].sum())

    ref = band(1000, 16000)
    if ref <= 0:
        return None, None  # silence: nothing to say

    def db(value: float) -> float:
        return round(10 * float(np.log10(value / ref + 1e-20)), 1)

    return db(band(16500, 19000)), db(band(19500, 21000))


def peaks(path: Path, channels: int) -> dict:
    """Over the whole track: peak, samples over full scale, samples in flat tops."""
    channels = max(1, channels)
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", str(channels), "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdout is not None and proc.stderr is not None
    frame = 4 * channels
    top = np.zeros(channels)
    over = 0
    # Сколько раз каждое значение начинало окно из FLAT_RUN равных отсчётов, по
    # каналам; пик канала известен только в конце, тогда и считается обрезка.
    runs: list[dict[float, int]] = [{} for _ in range(channels)]
    tail = np.zeros((0, channels), dtype=np.float32)  # FLAT_RUN - 1 frames carried across chunks
    rest = b""
    while chunk := proc.stdout.read(CHUNK):
        chunk = rest + chunk
        whole = len(chunk) - len(chunk) % frame
        rest = chunk[whole:]
        x = np.frombuffer(chunk[:whole], dtype=np.float32).reshape(-1, channels)
        a = np.abs(x)
        if len(a):
            top = np.maximum(top, a.max(axis=0))
        over += int(np.count_nonzero(a > 1.0))
        y = np.concatenate([tail, x])
        n = len(y) - FLAT_RUN + 1
        if n > 0:
            flat = np.ones((n, channels), dtype=bool)
            for k in range(1, FLAT_RUN):
                flat &= y[:n] == y[k : n + k]
            for c in range(channels):
                values, counts = np.unique(np.abs(y[:n, c][flat[:, c]]), return_counts=True)
                for v, count in zip(values.tolist(), counts.tolist(), strict=True):
                    runs[c][v] = runs[c].get(v, 0) + count
        tail = y[-(FLAT_RUN - 1) :]
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(err.strip()[-200:] or "ffmpeg could not decode the file")
    flat_samples = sum(
        count
        for c in range(channels)
        if top[c] >= CLIP_FLOOR
        for v, count in runs[c].items()
        if v >= top[c] - PEAK_TOLERANCE
    )
    return {
        "peak": round(float(top.max()), 3),
        "over_full_scale": over,
        "clipped_samples": flat_samples,
    }


def check(path: Path, root: Path) -> dict:
    row: dict = {"path": str(path.relative_to(root))}
    try:
        row.update(probe(path))
        row["above16_db"], row["above19_db"] = spectrum(path, row["duration"])
        row.update(peaks(path, row["channels"]))
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):  # ffprobe is read as text, ffmpeg's decode as bytes
            stderr = stderr.decode(errors="replace")
        row["error"] = (stderr or str(exc)).strip()[-200:]
        row["flags"], row["advice"] = ["unreadable"], ""
        return row
    except (RuntimeError, ValueError, OSError) as exc:
        row["error"] = str(exc)[-200:]
        row["flags"], row["advice"] = ["unreadable"], ""
        return row
    flags = []
    if row["above16_db"] is not None and row["above16_db"] < CUT_DB:
        flags.append("cut16")
    if row["clipped_samples"] >= CLIP_SAMPLES:
        flags.append("clipped")
    floor = LOW_BITRATE.get(row["codec"])
    if floor and row["bitrate"] and row["bitrate"] < floor:
        flags.append("low_bitrate")
    if row["sample_rate"] and row["sample_rate"] < 44100:
        flags.append("low_rate")
    row["flags"] = flags
    if flags and row["codec"] == "aac":
        row["advice"] = "upgrade_audio.py"
    elif "cut16" in flags:
        row["advice"] = "other source"
    else:
        row["advice"] = ""
    return row


def audio_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)


def audit(root: Path, limit: int | None = None, progress=None) -> list[dict]:
    files = audio_files(root)
    if limit is not None:
        files = files[:limit]
    rows = []
    for i, path in enumerate(files, 1):
        rows.append(check(path, root))
        if progress:
            progress(i, len(files), path, rows[-1])
    return rows


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the library's sound quality, read only.")
    parser.add_argument("library", nargs="?", type=Path, default=LIBRARY)
    parser.add_argument("--json", type=Path, help="write every row here, one JSON line per track")
    parser.add_argument("--limit", type=_positive, help="only the first N files (a quick look)")
    args = parser.parse_args(argv)
    # Пустой отчёт о неподключённой или не той фонотеке выглядел бы чистым.
    if not args.library.is_dir() or not audio_files(args.library):
        print(f"no audio files under {args.library}", file=sys.stderr)
        return 2

    out = args.json.open("w", encoding="utf-8", errors="backslashreplace") if args.json else None

    def progress(i: int, total: int, path: Path, row: dict) -> None:
        if out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
        if i % 50 == 0 or i == total:
            print(f"{i}/{total}", file=sys.stderr, flush=True)

    try:
        rows = audit(args.library, args.limit, progress)
    finally:
        if out:
            out.close()
    flagged = [r for r in rows if r["flags"]]
    codecs: dict[str, int] = {}
    for r in rows:
        codecs[r.get("codec") or "?"] = codecs.get(r.get("codec") or "?", 0) + 1
    print(f"{len(rows)} files: " + ", ".join(f"{c} {n}" for c, n in sorted(codecs.items())))
    print(f"flagged: {len(flagged)}")
    for r in sorted(flagged, key=lambda r: (r["advice"], r["path"])):
        print(
            f"  {','.join(r['flags']):22} {r.get('codec', '?'):5} {r.get('bitrate') or '?':>4}k "
            f">16k {r.get('above16_db')} dB, flat tops {r.get('clipped_samples')}  "
            f"[{r['advice'] or '-'}]  {r['path']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
