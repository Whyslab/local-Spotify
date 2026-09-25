"""Loudness for players that cannot turn a track down themselves.

On an iPhone the page cannot change an <audio> element's volume: Apple leaves
it to the hardware buttons. ReplayGain in the tags does nothing there. But an
Opus stream carries its own volume setting -- the OutputGain field of its
header, which a decoder applies as it decodes -- and that one the phone's own
decoder does not ignore.

The files stay as they are: the desktop window (GStreamer) ignores that field,
so a file with the gain written into it would play *louder* there, not
quieter. Instead the two bytes are set while the file is streamed, only for
a request that asks for it (``norm=1``). Everything else about the stream is
the file bit for bit, so its length and every Range request stay valid.

In an MP4 the Opus header is the ``dOps`` box (moov/trak/mdia/minf/stbl/
stsd/Opus/dOps): version, channels, pre-skip, input rate, then OutputGain as
a signed 16-bit big-endian number of 1/256 dB.
"""

from __future__ import annotations

import re
import struct
import threading
from collections.abc import Iterator
from pathlib import Path

from fastapi.responses import Response, StreamingResponse

CHUNK = 64 * 1024
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
_AUDIO_SAMPLE_ENTRY = 28  # bytes of fields before an audio sample entry's child boxes
_STSD_HEADER = 8  # version/flags + entry count

_CACHE: dict[tuple[str, int, int], int | None] = {}
_CACHE_LOCK = threading.Lock()


def _boxes(handle, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """(type, body offset, box end) of every box between start and end."""
    at = start
    while at + 8 <= end:
        handle.seek(at)
        head = handle.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head[:8])
        body = at + 8
        if size == 1:
            if len(head) < 16:
                return
            size = struct.unpack(">Q", head[8:16])[0]
            body = at + 16
        elif size == 0:
            size = end - at
        if size < body - at:
            return  # a broken size would loop forever
        yield kind, body, at + size
        at += size


def gain_offset(path: Path) -> int | None:
    """File offset of the Opus OutputGain field, or None if there is none."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    found = _find(path, stat.st_size)
    with _CACHE_LOCK:
        if len(_CACHE) > 4096:
            _CACHE.clear()
        _CACHE[key] = found
    return found


def _find(path: Path, size: int) -> int | None:
    try:
        with path.open("rb") as handle:

            def walk(start: int, end: int, depth: int) -> int | None:
                if depth > 10:
                    return None
                for kind, body, box_end in _boxes(handle, start, end):
                    if kind in _CONTAINERS:
                        hit = walk(body, box_end, depth + 1)
                    elif kind == b"stsd":
                        hit = walk(body + _STSD_HEADER, box_end, depth + 1)
                    elif kind == b"Opus":
                        hit = walk(body + _AUDIO_SAMPLE_ENTRY, box_end, depth + 1)
                    elif kind == b"dOps":
                        handle.seek(body)
                        field = handle.read(10)
                        # version 0, then 1+2+4 bytes before OutputGain
                        return body + 8 if len(field) == 10 and field[0] == 0 else None
                    else:
                        continue
                    if hit is not None:
                        return hit
                return None

            return walk(0, size, 0)
    except OSError:
        return None


def patched_gain(path: Path, offset: int, gain_db: float) -> bytes:
    """The two OutputGain bytes with gain_db added to what the file holds."""
    with path.open("rb") as handle:
        handle.seek(offset)
        (current,) = struct.unpack(">h", handle.read(2))
    wanted = max(-32768, min(32767, current + round(gain_db * 256)))
    return struct.pack(">h", wanted)


_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """(start, end inclusive), None for the whole file, "bad" for 416."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match:
        return None  # several ranges or another unit: the whole file is a valid answer
    first, last = match.groups()
    if not first and not last:
        return "bad"
    if not first:
        length = int(last)
        if length == 0:
            return "bad"
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return "bad"
    return start, min(end, size - 1)


def response(path: Path, gain_db: float, media_type: str, range_header: str | None) -> Response:
    """Serve `path` with its Opus OutputGain moved by gain_db, Range included.

    The caller has checked that the file is Opus in MP4 (gain_offset is not
    None); the file is read as it is streamed, so a track swapped on disk in
    the middle ends the stream rather than mixing two files silently.
    """
    offset = gain_offset(path)
    size = path.stat().st_size
    patch = patched_gain(path, offset, gain_db) if offset is not None else b""
    wanted = _parse_range(range_header, size)
    if isinstance(wanted, str):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = (0, size - 1) if wanted is None else wanted

    def body() -> Iterator[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            at = start
            while at <= end:
                chunk = handle.read(min(CHUNK, end - at + 1))
                if not chunk:
                    return
                if offset is not None and at < offset + 2 and offset < at + len(chunk):
                    data = bytearray(chunk)
                    for i, byte in enumerate(patch):
                        spot = offset + i - at
                        if 0 <= spot < len(data):
                            data[spot] = byte
                    chunk = bytes(data)
                yield chunk
                at += len(chunk)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Disposition": f"inline; filename*=UTF-8''{_quote(path.name)}",
    }
    status = 200
    if wanted is not None:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=status, media_type=media_type, headers=headers)


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name)
