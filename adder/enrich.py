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
    s = s.replace("ё", "е").replace("&", "and").replace("'", "").replace("’", "")
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


def describe(artist: str, title: str) -> tuple[TrackInfo, bool]:
    """Return metadata for a track plus whether it came from Deezer."""
    info = lookup(artist, title)
    return (info, True) if info else (fallback(artist, title), False)
