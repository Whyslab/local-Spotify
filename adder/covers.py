"""Playlist covers, kept as files first and replicated to Navidrome second.

Navidrome is where a Subsonic client reads a playlist cover from, so the
picture has to end up there. It must not be the only place it exists, though:
Navidrome ties a cover to a playlist id, and a playlist id is born from a file.
Rename the .m3u and Navidrome sees a different playlist -- the old one keeps the
cover and the new one arrives blank. Keeping the original here means that is a
re-upload rather than a loss, and it keeps the module honest about the rule the
rest of the project follows: the file is the truth, the database is a view.
"""

import logging
from pathlib import Path

from fastapi import HTTPException

from . import config, runtime

logger = logging.getLogger(__name__)

COVERS_DIR = runtime.PROJECT / "playlist-covers"

# Sniffed from the bytes, not taken from the file name or the client's header:
# both are supplied by the caller and neither is evidence of anything.
_MAGIC = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
)


def detect(data: bytes) -> tuple[str, str]:
    """Return (extension, media type) or refuse the upload."""
    for magic, extension, media_type in _MAGIC:
        if data.startswith(magic):
            return extension, media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise HTTPException(
        status_code=415,
        detail="Cover must be a JPEG, PNG or WebP image (detected from the file itself)",
    )


def _slot(name: str) -> Path:
    from . import playlists

    return COVERS_DIR / playlists.safe_name(name)


def store(name: str, data: bytes) -> str:
    """Save a cover locally. Returns the media type it was recognised as."""
    if len(data) > config.MAX_COVER_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Cover is {len(data) // 1024} KB; the limit is {config.MAX_COVER_BYTES // 1024} KB"
            ),
        )
    extension, media_type = detect(data)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in COVERS_DIR.glob(f"{_slot(name).name}.*"):
        stale.unlink(missing_ok=True)
    _slot(name).with_suffix(f".{extension}").write_bytes(data)
    logger.info("Cover stored for playlist %r (%s, %d bytes)", name, media_type, len(data))
    return media_type


def cover_file(name: str) -> Path | None:
    matches = sorted(COVERS_DIR.glob(f"{_slot(name).name}.*")) if COVERS_DIR.exists() else []
    return matches[0] if matches else None


def read_cover(name: str) -> tuple[bytes, str] | None:
    """The stored cover as (bytes, filename), or None."""
    path = cover_file(name)
    if path is None:
        return None
    return path.read_bytes(), path.name


def media_type(name: str) -> str | None:
    path = cover_file(name)
    if path is None:
        return None
    return {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
        path.suffix.lstrip("."), "application/octet-stream"
    )


def rename(old: str, new: str) -> None:
    path = cover_file(old)
    if path is None:
        return
    path.rename(_slot(new).with_suffix(path.suffix))


def delete(name: str) -> None:
    path = cover_file(name)
    if path is not None:
        path.unlink(missing_ok=True)
