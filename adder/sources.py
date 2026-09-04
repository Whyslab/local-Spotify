"""Ways of naming a track that is not in the library yet.

Four of them: a YouTube link (which the rest of the pipeline already handles),
a search phrase, a YouTube playlist, and a Spotify playlist. The last one is
the awkward case and most of this file is about it.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from . import ingest

logger = logging.getLogger(__name__)

SPOTIFY_EMBED = "https://open.spotify.com/embed/playlist/{playlist_id}"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
TIMEOUT = 20

# The embed endpoint stops at 100 entries and does not say how many there are
# in total -- checked against a playlist of 1085: no count field anywhere in
# the page. So a long playlist cannot even be reported as "100 of N"; the
# honest message is that this is as far as the source goes.
SPOTIFY_EMBED_LIMIT = 100

# How close two durations have to be to count as the same recording. Matching
# on title alone pulled in live versions and other people's covers back in
# August; the rule was tightened twice before it held. Three seconds allows for
# a trimmed intro, not for a different arrangement.
DURATION_TOLERANCE_SECONDS = 3


@dataclass(frozen=True)
class Candidate:
    """One track named by a source, before it is known to exist on YouTube."""

    artist: str
    title: str
    duration: float | None = None

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces -- for comparison only."""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", (text or "").lower())
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def search_youtube(query: str, limit: int = 8) -> list[dict]:
    """Search YouTube and return what was found, without downloading anything.

    The point is to let a person choose. Two uploads of the same song differ in
    length, in channel, and in whether they are the studio version -- guessing
    between them is what produced a library full of live takes last time.
    """
    query = (query or "").strip()
    if not query:
        return []

    result = ingest.run_yt_dlp(
        [
            *ingest.ytdlp_base(),
            "-J",
            "--flat-playlist",
            f"ytsearch{max(1, min(limit, 20))}:{query}",
        ],
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-300:] or "yt-dlp search failed")

    payload = json.loads(result.stdout or "{}")
    found = []
    for entry in payload.get("entries") or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        found.append(
            {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": entry.get("title") or "",
                "channel": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration"),
            }
        )
    return found


def youtube_playlist(url: str) -> list[str]:
    """Every video URL in a YouTube playlist."""
    result = ingest.run_yt_dlp(
        [*ingest.ytdlp_base(), "-J", "--flat-playlist", url],
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-300:] or "yt-dlp could not read the playlist")

    payload = json.loads(result.stdout or "{}")
    urls = []
    for entry in payload.get("entries") or []:
        video_id = entry.get("id")
        if video_id:
            urls.append(f"https://www.youtube.com/watch?v={video_id}")
    return urls


def best_youtube_match(candidate: Candidate) -> dict | None:
    """Pick the YouTube result that is the same recording, or admit there is none.

    Three conditions, all required: the artist appears, the title appears, and
    the length is within a few seconds. Any two of them are not enough -- title
    and artist alone match a live version of the same song by the same person.
    """
    try:
        results = search_youtube(candidate.query, limit=8)
    except Exception as exc:
        logger.info("Search failed for %r: %s", candidate.query, exc)
        return None

    wanted_artist = _normalise(candidate.artist)
    wanted_title = _normalise(candidate.title)

    for result in results:
        haystack = _normalise(f"{result['title']} {result['channel']}")
        if wanted_title and wanted_title not in haystack:
            continue
        if wanted_artist and not any(word in haystack for word in wanted_artist.split()):
            continue
        if (
            candidate.duration
            and result.get("duration")
            and abs(float(result["duration"]) - float(candidate.duration))
            > DURATION_TOLERANCE_SECONDS
        ):
            continue
        return result
    return None


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------


def spotify_playlist_id(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if (parsed.hostname or "").lower().rstrip(".") not in {"open.spotify.com", "spotify.com"}:
        return None
    match = re.search(r"/playlist/([A-Za-z0-9]+)", parsed.path)
    return match.group(1) if match else None


def spotify_playlist(url: str) -> tuple[list[Candidate], bool]:
    """Read a public Spotify playlist without an API key.

    The Web API is unusable here: every endpoint answers 403 without an active
    Premium subscription on the app owner's account, including reading a public
    playlist. The embed page carries the track list as JSON and needs no
    credentials at all -- but it stops at 100 entries.

    Returns the tracks and whether the list was cut short.
    """
    playlist_id = spotify_playlist_id(url)
    if not playlist_id:
        raise ValueError("Not a Spotify playlist link")

    response = requests.get(
        SPOTIFY_EMBED.format(playlist_id=playlist_id),
        headers={"User-Agent": BROWSER_UA},
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        raise RuntimeError("Spotify changed the embed page; the track list is not where it was")

    def find_track_list(node):
        if isinstance(node, dict):
            if isinstance(node.get("trackList"), list):
                return node["trackList"]
            for value in node.values():
                found = find_track_list(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find_track_list(value)
                if found is not None:
                    return found
        return None

    track_list = find_track_list(json.loads(match.group(1))) or []
    candidates = [
        Candidate(
            artist=entry.get("subtitle") or "",
            title=entry.get("title") or "",
            duration=(entry.get("duration") or 0) / 1000 or None,
        )
        for entry in track_list
        if entry.get("title")
    ]
    return candidates, len(candidates) >= SPOTIFY_EMBED_LIMIT
