#!/usr/bin/env python3
"""Replace library tracks with YouTube's original Opus stream, bit for bit.

Most of the library went through Opus -> AAC on the way in: the band survived,
but peaks above full scale were flattened (hundreds of thousands of samples in
a loud track), and a second lossy pass was added on top. Newer downloads took
YouTube's AAC stream, which has nothing above 16 kHz. Both are worse than the
Opus stream YouTube itself serves, which is what new downloads keep now.

This finds each track on YouTube again, downloads the Opus stream, and swaps
it in only when it is provably the same recording:

* the video's length is within DURATION_SLACK of the file's;
* its title does not name another version (clean, sped up, remix, ...) that
  the file's own title does not;
* once aligned, the decoded audio correlates with the current file at
  MIN_CORRELATION or more overall, and at MIN_WINDOW_CORRELATION or more in
  every WINDOW_SECONDS stretch that is not near-silent. Measured on 13 real
  pairs: 0.98 to 1.00 overall, worst second 0.90 (most above 0.99). A
  different master reached 0.93 overall, another version 0.42 in its worst
  second, and a clean edit with four bleeps 0.987 overall -- which is why the
  seconds are checked as well: a bleep takes its second down to 0.5.

Where the link is known (the SOURCE_URL tag, the task that downloaded it, the
Spotify import CSV) it is tried first, otherwise YouTube search. Uploaded
files are never touched -- they may be better than anything on YouTube --
and neither is a track for which YouTube has no Opus stream.

The swap keeps everything the library cares about: every tag and the cover
are copied over, ReplayGain is measured again, the modification time (the
"added" date) is kept, and audio analysis rows move to the new file hash. The
old file is copied to BACKUP before it is replaced, under the same relative
path, so any track can be put back by hand.

Progress lives in STATE and each track is written as soon as it is decided,
so the run can be stopped and started again. It stops on its own when YouTube
starts asking for a sign-in or rate-limits: pressing on would only lengthen
the ban. At the end Navidrome is asked for a full rescan: the kept mtime
would otherwise let its quick scan skip the new files.

    .venv/bin/python scripts/upgrade_audio.py --limit 5        # a first taste
    .venv/bin/python scripts/upgrade_audio.py --dry-run        # check, change nothing
    .venv/bin/python scripts/upgrade_audio.py                  # the whole library
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy comes with the analysis extras, imported only when a run starts
    import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from adder import config, ingest, library, loudness  # noqa: E402

logger = logging.getLogger("upgrade_audio")

DATA = Path.home() / ".local" / "share" / "local-Spotify" / "opus-upgrade"
BACKUP = DATA / "backup"
STATE = DATA / "state.json"
DRY_STATE = DATA / "state-dry-run.json"
LOCK = DATA / "lock"
RESCAN_PENDING = DATA / "rescan-pending"
WORK = DATA / "work"

RATE = 8000  # enough to tell recordings apart, cheap to hold for a 40-minute mix
MIN_CORRELATION = 0.97
MIN_WINDOW_CORRELATION = 0.8
WINDOW_SECONDS = 1
QUIET_WINDOW = 0.05  # RMS below 5 % of the track's own is left out of the window check
LAG_EXCERPT_SECONDS = 120
DURATION_SLACK = 3.0
SEARCH_RESULTS = 5
PAUSE_SECONDS = 4.0
MAX_ATTEMPTS = 3
MAX_UNREACHABLE_IN_A_ROW = 5
MP4_SOURCE = "----:com.apple.iTunes:SOURCE_URL"

# Words in a video title that mean another version of the song. Rejected
# unless the file's own title says the same.
OTHER_VERSIONS = (
    "clean", "censored", "radio edit", "sped up", "speed up", "slowed", "nightcore",
    "remix", "live", "acoustic", "instrumental", "karaoke", "cover", "8d", "reverb",
    "ремикс", "ускор", "замедл", "кавер", "минус",
)  # fmt: skip


class Blocked(RuntimeError):
    """YouTube refuses to answer; the run has to stop, not skip."""


# Failures that say something about YouTube or this machine, not the video:
# a 403 wave, a broken extractor, a missing ffmpeg. A removed, private or
# region-blocked video is that video's own problem and falls outside.
SETUP_FAILURES = (
    "403", "forbidden", "unable to extract", "nsig", "po token", "signature",
    "requested format is not available", "ffmpeg", "ffprobe",
)  # fmt: skip


class Unreachable(RuntimeError):
    """A request failed for a reason that says nothing about the track."""


# ---------------------------------------------------------------------------
# Comparing two files
# ---------------------------------------------------------------------------


def decode_mono(path: Path, rate: int = RATE) -> np.ndarray:
    import numpy as np

    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate),
         "-f", "f32le", "-"],
        capture_output=True, timeout=600,
    ).stdout  # fmt: skip
    return np.frombuffer(out, np.float32)


def find_lag(a: np.ndarray, b: np.ndarray, limit: int) -> int:
    """Offset of b against a, in samples, within +-limit, from the openings only.

    A full-length FFT of a 40-minute mix needs gigabytes; the offset between two
    copies of one recording is a matter of seconds and shows in the first two
    minutes as well as anywhere.
    """
    import numpy as np

    n_excerpt = LAG_EXCERPT_SECONDS * RATE
    x = a[: n_excerpt + limit].astype(np.float64)
    y = b[: n_excerpt + limit].astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(y, n)), n)
    lags = np.concatenate([np.arange(0, limit + 1), np.arange(-limit, 0)])
    values = np.concatenate([c[: limit + 1], c[n - limit :]])
    return int(lags[int(np.argmax(values))])


def similarity(old: Path, new: Path) -> tuple[float, float, float]:
    """(overall correlation, worst window correlation, offset in seconds)."""
    import numpy as np

    a, b = decode_mono(old), decode_mono(new)
    if not len(a) or not len(b):
        return 0.0, 0.0, 0.0
    lag = find_lag(a, b, int((DURATION_SLACK + 2) * RATE))
    x, y = (a[lag:], b) if lag >= 0 else (a, b[-lag:])
    m = min(len(x), len(y))
    x, y = x[:m], y[:m]

    def corr(p: np.ndarray, q: np.ndarray) -> float:
        d = float(np.sqrt(np.dot(p, p) * np.dot(q, q)))
        return float(np.dot(p, q)) / d if d else 0.0

    overall = corr(x, y)
    step = WINDOW_SECONDS * RATE
    loud = float(np.sqrt(float(np.dot(x, x)) / m)) if m else 0.0
    worst = 1.0
    for start in range(0, m - step + 1, step):
        p, q = x[start : start + step], y[start : start + step]
        if float(np.sqrt(float(np.dot(p, p)) / len(p))) < QUIET_WINDOW * loud:
            continue
        worst = min(worst, corr(p, q))
    return overall, worst, lag / RATE


def codec_of(path: Path) -> str:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    return (probe.stdout.strip().splitlines() or [""])[0].split(",")[0].strip().lower()


def names_another_version(video_title: str, own_title: str) -> bool:
    """Whole words only: "live" is not in "Olivia", nor "cover" in "discover"."""
    import re

    def has(word: str, text: str) -> bool:
        # Russian entries are stems ("ускор" -> ускоренная), English ones words.
        end = r"(?!\w)" if word.isascii() else ""
        return re.search(rf"(?<!\w){re.escape(word)}{end}", text) is not None

    theirs = video_title.lower().replace("-", " ")
    ours = own_title.lower().replace("-", " ")
    return any(has(word, theirs) and not has(word, ours) for word in OTHER_VERSIONS)


# ---------------------------------------------------------------------------
# Where a track came from
# ---------------------------------------------------------------------------


def youtube_id(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.lstrip("/") or None
    if "youtube.com" in host:
        found = parse_qs(parsed.query).get("v")
        return found[0] if found else None
    return None


def known_links(db_path: Path) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Links from finished tasks by library path, and (artist, title) of uploads.

    Uploads are told by name, not by path: moving a track to another artist
    folder from the panel leaves the task row with the old path.
    """
    links: dict[str, str] = {}
    uploads: list[tuple[str, str]] = []
    if not db_path.exists():
        return links, uploads
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        rows = con.execute(
            "SELECT url, artist, title, result_path FROM tasks WHERE status = 'done'"
        ).fetchall()
    for url, artist, title, path in rows:
        if (url or "").startswith("file:"):
            uploads.append(((artist or "").lower(), (title or "").lower()))
        elif path and youtube_id(url or ""):
            links[path] = url
    return links, uploads


def is_upload(tags: dict, uploads: list[tuple[str, str]]) -> bool:
    """By title alone: the file's artist comes from Deezer (Tiësto for an
    upload named tiesto). A false match only leaves a track as it is."""
    title = tags["title"].lower()
    return any(t and t == title for _, t in uploads)


def csv_links(path: Path) -> dict[tuple[str, str], str]:
    """(artist, title) -> link from the Spotify import, both lowercased."""
    found: dict[tuple[str, str], str] = {}
    if not path.exists():
        return found
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            url = row.get("youtube_url") or ""
            artists = (row.get("artists") or "").split(",")[0].strip().lower()
            if youtube_id(url) and artists:
                found[(artists, (row.get("name") or "").strip().lower())] = url
    return found


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def make_client():
    import yt_dlp  # type: ignore[import-untyped]

    options: dict = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": ingest.AUDIO_FORMAT,
        "outtmpl": str(WORK / "%(id)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}],
    }
    if config.COOKIES_FROM_BROWSER:
        browser, _, profile = config.COOKIES_FROM_BROWSER.partition(":")
        options["cookiesfrombrowser"] = (browser, profile or None, None, None)
    return yt_dlp.YoutubeDL(options)


def _judge(exc: Exception) -> None:
    """Raise Blocked or Unreachable when the failure is not the video's own.

    What is left -- removed, private, age-gated, no such format -- is about
    this one video, and the caller moves on to the next candidate.
    """
    text = str(exc).lower()
    if "confirm your age" in text or "age-restricted" in text:
        return
    if "not a bot" in text or "sign in to confirm" in text:
        raise Blocked(str(exc)[:200]) from exc
    kind = ingest.classify_error(str(exc))
    if kind == "rate_limited":
        raise Blocked(str(exc)[:200]) from exc
    if kind == "network_error":
        raise Unreachable(str(exc)[:200]) from exc


def search(client, artist: str, title: str, duration: float) -> list[tuple[str, str]]:
    """(id, title) of search results whose length is close to the file's."""
    query = f"ytsearch{SEARCH_RESULTS}:{artist} {title}"
    try:
        found = client.extract_info(query, download=False, process=False)
        # The entries are lazy: the requests happen while they are read, so
        # reading them belongs inside the try as well.
        entries = list(itertools.islice((found or {}).get("entries") or [], SEARCH_RESULTS))
    except Exception as exc:
        _judge(exc)
        return []
    return [
        (entry["id"], entry.get("title") or "")
        for entry in entries
        if entry.get("id") and abs((entry.get("duration") or 0) - duration) <= DURATION_SLACK
    ]


class NotDownloaded(RuntimeError):
    """This candidate could not be fetched; `setup` tells whose fault it was."""

    def __init__(self, message: str, setup: bool):
        super().__init__(message)
        self.setup = setup


def download(client, video_id: str) -> tuple[Path, str] | None:
    """The video's audio as .m4a, and the video's title.

    None when the video itself is gone or unusable; NotDownloaded(setup=True)
    when the failure looks like YouTube's or this machine's.
    """
    for stale in WORK.glob(f"{video_id}.*"):
        stale.unlink(missing_ok=True)
    try:
        info = client.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    except Exception as exc:
        _judge(exc)
        text = str(exc).lower()
        logger.info("  %s: not downloaded (%s)", video_id, str(exc).splitlines()[0][:120])
        if any(word in text for word in SETUP_FAILURES):
            raise NotDownloaded(str(exc)[:200], setup=True) from exc
        return None
    if not info:
        return None
    produced = sorted(
        p for p in WORK.glob(f"{video_id}.*") if p.suffix not in ingest.YTDLP_LEFTOVERS
    )
    if not produced:
        return None
    try:
        return ingest.ensure_m4a(produced[0]), info.get("title") or ""
    except RuntimeError as exc:
        logger.info("  %s: %s", video_id, exc)
        return None


# ---------------------------------------------------------------------------
# The swap
# ---------------------------------------------------------------------------


def carry_tags(old: Path, new: Path, url: str) -> None:
    """Every tag and the cover of `old`, onto `new`, plus the link it came from."""
    from mutagen.mp4 import MP4, MP4FreeForm

    source = MP4(old)
    target = MP4(new)
    if target.tags is None:
        target.add_tags()
    assert target.tags is not None
    target.tags.clear()
    for key, value in (source.tags or {}).items():
        if key.lower() == "----:com.apple.itunes:itunsmpb":
            continue  # AAC's encoder delay; it would be wrong for Opus
        target.tags[key] = value
    target.tags[MP4_SOURCE] = [MP4FreeForm(url.encode())]
    target.save()


def _stamp(stat: os.stat_result) -> tuple[int, int, int, int]:
    # ctime as well as mtime: a tag edit from the panel keeps mtime on purpose
    # and often the size too, but no write can keep ctime.
    return stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino


def swap(old: Path, new: Path, url: str, root: Path, db_path: Path) -> None:
    before = old.stat()
    old_hash = loudness.file_hash(old)
    carry_tags(old, new, url)
    loudness.apply(new)
    relative = old.relative_to(root)
    backup = BACKUP / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    # An existing backup is the original from an earlier pass; it is never replaced.
    if not backup.exists():
        shutil.copy2(old, backup)
    # The file must not have changed while this track was being worked on: a
    # tag edit from the panel would otherwise be lost under the new copy.
    if _stamp(old.stat()) != _stamp(before):
        raise RuntimeError("changed during the upgrade; left as it was")
    os.chmod(new, before.st_mode & 0o7777)
    os.utime(new, ns=(before.st_atime_ns, before.st_mtime_ns))
    staged = old.with_name(f".{old.name}.upgrading")
    shutil.move(str(new), staged)
    RESCAN_PENDING.parent.mkdir(parents=True, exist_ok=True)
    RESCAN_PENDING.touch()  # before the replace: a kill right after it must not lose the rescan
    os.replace(staged, old)
    try:
        _move_analysis(db_path, str(relative), old_hash, loudness.file_hash(old))
    except sqlite3.Error as exc:
        # The audio is swapped already; a missed row only means it is measured again.
        logger.warning("  analysis row not moved: %s", exc)


def _move_analysis(db_path: Path, relative: str, before: str, after: str) -> None:
    if not db_path.exists():
        return
    with sqlite3.connect(db_path, timeout=30) as con:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audio_features'"
        ).fetchone()
        if has:
            con.execute(
                "UPDATE audio_features SET sha256 = ? WHERE path = ? AND sha256 = ?",
                (after, relative, before),
            )


def sweep_leftovers(root: Path) -> None:
    """A crash between staging and replacing leaves `.name.upgrading` beside the
    untouched original; it is only a copy."""
    for staged in root.rglob(".*.upgrading"):
        staged.unlink(missing_ok=True)
    for stale in WORK.glob("*"):
        if stale.is_file():
            stale.unlink(missing_ok=True)


def rescan_navidrome() -> None:
    """Pending since the first swap, cleared only once Navidrome has agreed:
    a run stopped with Ctrl+C still gets its rescan on the next start."""
    from adder import navidrome

    try:
        navidrome.full_scan()
    except Exception as exc:
        logger.warning("Navidrome rescan not started (%s); start one from its web page", exc)
        return
    RESCAN_PENDING.unlink(missing_ok=True)
    logger.info("Navidrome: full rescan started")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    partial.replace(path)


def take_lock():
    """One run at a time: two would meet on the same track, and the second
    would back up the first one's Opus in place of the original."""
    import fcntl

    DATA.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def candidates_for(
    relative: str, tags: dict, links: dict[str, str], by_name: dict[tuple[str, str], str]
) -> list[str]:
    """Known links first, in the order they are trusted."""
    urls = []
    for url in (
        tags.get("source") or "",
        links.get(relative, ""),
        by_name.get((tags["artist"].split(" • ")[0].lower(), tags["title"].lower()), ""),
    ):
        if youtube_id(url) and url not in urls:
            urls.append(url)
    return urls


def upgrade_one(client, path: Path, root: Path, links, by_name, db_path: Path, dry: bool) -> dict:
    relative = str(path.relative_to(root))
    tags = library.read_tags(path)
    duration = tags.get("duration") or 0
    tried: set[str] = set()
    ids = (youtube_id(u) for u in candidates_for(relative, tags, links, by_name))
    known = [(i, "") for i in ids if i]
    no_opus = False
    compared = unavailable = 0
    # The artist is part of a video's title: "Clean Bandit - Rather Be" is not a clean edit.
    own = f"{tags['artist']} {tags['title']}"
    for round_ids, how in ((known, "known link"), (None, "search")):
        if round_ids is None:
            artist = tags["artist"].split(" • ")[0]
            round_ids = search(client, artist, tags["title"], duration)
        for video_id, listed_title in round_ids:
            if video_id in tried:
                continue
            tried.add(video_id)
            if how == "search" and names_another_version(listed_title, own):
                logger.info("  %s: another version (%s)", video_id, listed_title[:60])
                continue
            try:
                got = download(client, video_id)
            except NotDownloaded:
                unavailable += 1
                continue
            if got is None:
                continue
            new, video_title = got
            compared += 1
            try:
                # A known link is the very video the file came from; the audio
                # comparison below is the stronger test there.
                if how == "search" and names_another_version(video_title, own):
                    logger.info("  %s: another version (%s)", video_id, video_title[:60])
                    continue
                if codec_of(new) != "opus":
                    no_opus = True
                    logger.info("  %s: no Opus stream on YouTube", video_id)
                    continue
                overall, worst, lag = similarity(path, new)
                length = library.read_tags(new).get("duration") or 0
                fits = (
                    overall >= MIN_CORRELATION
                    and worst >= MIN_WINDOW_CORRELATION
                    and abs(length - duration) <= DURATION_SLACK
                )
                logger.info(
                    "  %s (%s): correlation %.3f, worst second %.3f, offset %+.2fs,"
                    " length %+.1fs%s",
                    video_id, how, overall, worst, lag, length - duration,
                    "" if fits else " -- not it",
                )  # fmt: skip
                if not fits:
                    continue
                result = {
                    "status": "checked" if dry else "upgraded",
                    "video": video_id,
                    "correlation": round(overall, 4),
                    "worst_window": round(worst, 4),
                }
                if not dry:
                    swap(path, new, f"https://www.youtube.com/watch?v={video_id}", root, db_path)
                return result
            finally:
                new.unlink(missing_ok=True)
    if not compared and unavailable:
        # Nothing could be looked at, for reasons that are YouTube's (a 403
        # wave, an outdated yt-dlp), not the track's. Not "not found".
        raise Unreachable(f"{unavailable} candidates failed for reasons not their own")
    if no_opus:
        return {"status": "no opus on youtube", "tried": sorted(tried)}
    return {"status": "not found", "tried": sorted(tried)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO / "adder" / "adder.db")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many tracks")
    parser.add_argument("--dry-run", action="store_true", help="find and check, replace nothing")
    parser.add_argument("--retry", action="store_true", help="try the 'not found' ones again")
    parser.add_argument("--only", default=None, help="one track, by library-relative path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    lock = take_lock()
    if lock is None:
        logger.error("Another run is going on (%s); this one stops.", LOCK)
        return 3
    root = args.library or config.LIBRARY
    WORK.mkdir(parents=True, exist_ok=True)
    sweep_leftovers(root)
    if RESCAN_PENDING.exists() and not args.dry_run:
        rescan_navidrome()  # left over from a run that was stopped
    links, uploads = known_links(args.db)
    by_name = csv_links(REPO / "missing_youtube.csv")
    # A dry run keeps its own notes: its "not found" must not hide a track
    # from the real run.
    state_path = DRY_STATE if args.dry_run else STATE
    state = load_state(state_path)
    client = make_client()

    tracks = sorted(root.rglob("*.m4a"))
    if args.only:
        tracks = [root / args.only]
    done = changed = stopped = 0
    unreachable_in_a_row = 0
    for path in tracks:
        relative = str(path.relative_to(root))
        entry = state.get(relative, {})
        previous = entry.get("status")
        if previous in ("upgraded", "upload, kept", "already opus", "not aac, kept"):
            continue
        if previous in ("not found", "no opus on youtube") and not args.retry:
            continue
        if previous == "failed" and entry.get("attempts", 0) >= MAX_ATTEMPTS and not args.retry:
            continue
        try:
            tags = library.read_tags(path)
            codec = codec_of(path)
        except Exception as exc:
            logger.warning("%s: unreadable for now (%s), skipped", relative, exc)
            continue
        if is_upload(tags, uploads):
            state[relative] = {"status": "upload, kept"}
            continue
        if codec == "opus":
            state[relative] = {"status": "already opus"}
            continue
        if not codec:
            logger.warning("%s: ffprobe could not read it now, skipped", relative)
            continue
        if codec != "aac":
            # ALAC is lossless and was put here on purpose; Opus would be a step down.
            state[relative] = {"status": "not aac, kept", "codec": codec}
            continue
        if args.limit is not None and done >= args.limit:
            break
        done += 1
        logger.info("%s", relative)
        try:
            state[relative] = upgrade_one(client, path, root, links, by_name, args.db, args.dry_run)
            unreachable_in_a_row = 0
        except Blocked as exc:
            logger.error("YouTube is refusing requests, stopping here: %s", exc)
            stopped = 2
            break
        except Unreachable as exc:
            # Not the track's fault: no attempt is counted against it.
            logger.warning("  unreachable: %s", exc)
            state[relative] = {**entry, "status": "failed", "error": str(exc)[:200]}
            unreachable_in_a_row += 1
            if unreachable_in_a_row >= MAX_UNREACHABLE_IN_A_ROW:
                logger.error(
                    "%d tracks in a row could not be reached; stopping", unreachable_in_a_row
                )
                save_state(state, state_path)
                stopped = 2
                break
        except Exception as exc:
            logger.warning("  failed: %s", exc)
            unreachable_in_a_row = 0
            state[relative] = {
                "status": "failed",
                "error": str(exc)[:200],
                "attempts": entry.get("attempts", 0) + 1,
            }
        if state[relative]["status"] == "upgraded":
            changed += 1
        logger.info("  -> %s", state[relative]["status"])
        save_state(state, state_path)
        time.sleep(PAUSE_SECONDS)

    save_state(state, state_path)
    counts: dict[str, int] = {}
    for item in state.values():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    logger.info("Totals: %s", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if RESCAN_PENDING.exists() and not args.dry_run:
        rescan_navidrome()
    return stopped


if __name__ == "__main__":
    sys.exit(main())
