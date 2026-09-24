"""Metadata enrichment for newly downloaded tracks.

yt-dlp only knows what the YouTube page says: a title, an uploader, a thumbnail.
That is not enough to file a track correctly - it has no album, no track number
and no idea that "Song (feat. X)" involves two separate artists.

This module asks Deezer, which is free and needs no API key, and returns the
missing pieces. Every failure path returns ``None`` so a download never breaks
just because a lookup did.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEEZER = "https://api.deezer.com"
TIMEOUT = 15
_UA = {"User-Agent": "local-Spotify/1.0"}


@dataclass
class TrackInfo:
    """What Deezer knows about a track that YouTube does not."""

    album: str
    artists: list[str] = field(default_factory=list)
    track_number: int | None = None
    track_total: int = 0
    disc_number: int = 1
    date: str = ""
    cover_url: str = ""


def normalize(s: str) -> str:
    """Fold a title or artist down to what two spellings of it have in common."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    # Надстрочные знаки — прочь, а не в пробел: NFKD раскладывает «ё» на «е»
    # и знак, и прежняя замена «ё»→«е» после неё уже ничего не находила —
    # «Ёлка» становилась «е лка» и не сходилась с «Елкой» у Deezer.
    s = "".join(char for char in s if not unicodedata.combining(char))
    s = s.replace("&", "and").replace("'", "").replace("’", "")
    s = re.sub(r"\((?:feat|ft|with)\.?[^)]*\)", " ", s)
    s = re.sub(r"\b(?:feat|ft)\.?\s.*$", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception:
        return None


def split_artists(artist: str) -> list[str]:
    """Split a joined artist string into individual names.

    Handles the separators that show up in YouTube metadata and in the
    Spotify exports the library was originally built from.
    """
    if not artist:
        return []
    parts = re.split(r"\s*[;,/&]\s*|\s+(?:feat|ft|with|x)\.?\s+", artist, flags=re.IGNORECASE)
    seen, out = set(), []
    for p in (p.strip() for p in parts):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def lookup(artist: str, title: str) -> TrackInfo | None:
    """Find a track on Deezer. Returns None when it is not found or the network fails."""
    lead = (split_artists(artist) or [artist])[0]
    query = urllib.parse.quote(f'artist:"{lead}" track:"{title}"')
    found = _get(f"{DEEZER}/search?q={query}&limit=5")
    if not found or not found.get("data"):
        return None

    want = normalize(title)
    match = next((c for c in found["data"] if normalize(c.get("title", "")) == want), None)
    if match is None:
        return None

    full = _get(f"{DEEZER}/track/{match['id']}")
    if not full or "album" not in full:
        return None

    album = full["album"]
    total = 0
    detail = _get(f"{DEEZER}/album/{album['id']}")
    if detail:
        total = detail.get("nb_tracks") or 0

    return TrackInfo(
        album=album.get("title") or title,
        artists=[c["name"] for c in full.get("contributors", [])] or split_artists(artist),
        track_number=full.get("track_position"),
        track_total=total,
        disc_number=full.get("disk_number") or 1,
        date=full.get("release_date") or "",
        cover_url=album.get("cover_xl") or album.get("cover_big") or "",
    )


def fallback(artist: str, title: str) -> TrackInfo:
    """When Deezer draws a blank, treat the track as its own single.

    Naming the album after the track is what makes it show up as a standalone
    release with its own artwork, instead of being swept into a shared bucket
    with every other unidentified track by the same artist.
    """
    return TrackInfo(album=title, artists=split_artists(artist) or [artist or "Unknown Artist"])


# ---------------------------------------------------------------------------
# MusicBrainz, when Deezer does not know the track
# ---------------------------------------------------------------------------
#
# Deezer misses a fair share of what is on YouTube - live cuts, older and
# smaller releases - and every miss became a "single" named after the track.
# MusicBrainz is free, needs no key and covers much of what Deezer lacks. Its
# terms ask for at most one request per second and a User-Agent naming the
# application and a contact, both honoured here. Covers come from the Cover
# Art Archive, keyed by the same release id.

MUSICBRAINZ = "https://musicbrainz.org/ws/2"
COVER_ART = "https://coverartarchive.org/release/{mbid}/front-500"
_MB_UA = {
    "User-Agent": "local-Spotify/1.0 ( https://github.com/Whyslab/local-Spotify )",
    "Accept": "application/json",
}
_MB_INTERVAL = 1.1
_mb_lock = threading.Lock()
_mb_last = 0.0
# A search result this far below MusicBrainz's own best score is not the song.
MIN_SCORE = 85


def _mb_get(url: str) -> dict | None:
    global _mb_last
    with _mb_lock:
        wait = _mb_last + _MB_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _mb_last = time.monotonic()
    try:
        request = urllib.request.Request(url, headers=_MB_UA)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception:
        return None


def _mb_quote(text: str) -> str:
    # Lucene query syntax: quotes and backslashes inside a phrase are escaped.
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _pick_release(releases: list[dict]) -> dict | None:
    """The release a listener would call "the album": official, an album, earliest."""

    def rank(release: dict) -> tuple:
        group = release.get("release-group") or {}
        return (
            release.get("status") != "Official",
            group.get("primary-type") != "Album",
            bool(group.get("secondary-types")),  # compilations, live, soundtracks last
            release.get("date") or "9999",
        )

    return min(releases, key=rank) if releases else None


def musicbrainz_lookup(artist: str, title: str) -> TrackInfo | None:
    """Find a recording on MusicBrainz. None when it is not found or the network fails."""
    lead = (split_artists(artist) or [artist])[0]
    query = f'recording:"{_mb_quote(title)}" AND artist:"{_mb_quote(lead)}"'
    found = _mb_get(f"{MUSICBRAINZ}/recording?fmt=json&limit=5&query={urllib.parse.quote(query)}")
    if not found:
        return None

    want_title, want_artist = normalize(title), normalize(lead)
    for recording in found.get("recordings") or []:
        if (recording.get("score") or 0) < MIN_SCORE:
            continue
        if normalize(recording.get("title", "")) != want_title:
            continue
        credits = [c.get("name", "") for c in recording.get("artist-credit") or []]
        if want_artist and want_artist not in {normalize(c) for c in credits}:
            continue
        release = _pick_release(recording.get("releases") or [])
        if release is None:
            continue

        media = (release.get("media") or [{}])[0]
        track = (media.get("track") or [{}])[0]
        try:
            number = int(track.get("number") or track.get("position") or 0) or None
        except ValueError:
            number = track.get("position")
        return TrackInfo(
            album=release.get("title") or title,
            artists=[c for c in credits if c] or split_artists(artist),
            track_number=number,
            track_total=media.get("track-count") or 0,
            disc_number=media.get("position") or 1,
            date=release.get("date") or "",
            cover_url=COVER_ART.format(mbid=release["id"]) if release.get("id") else "",
        )
    return None


def describe(artist: str, title: str) -> tuple[TrackInfo, str]:
    """Metadata for a track, and where it came from: deezer, musicbrainz or fallback."""
    info = lookup(artist, title)
    if info:
        return info, "deezer"
    info = musicbrainz_lookup(artist, title)
    if info:
        return info, "musicbrainz"
    return fallback(artist, title), "fallback"
