"""YouTube -> Navidrome: HTTP surface. Everything else lives in its own module.

This file is deliberately thin: it wires FastAPI to the pieces and does no work
of its own. The library lives in :mod:`adder.library`, the processing pipeline
in :mod:`adder.ingest`, the queue and its retry policy in :mod:`adder.queue`.
"""

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, covers, db, ingest, library, navidrome, playlists, runtime, signing
from . import queue as task_queue

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> bool:
    """Verify API token (Problem #19).

    config.py refuses to start the app at all if API_TOKEN is unset, so
    there is no "no token configured" case to allow through here.
    secrets.compare_digest avoids leaking the token via a timing side
    channel on the comparison.
    """
    if credentials is None or not secrets.compare_digest(credentials.credentials, config.API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize runtime state and gracefully stop workers."""
    runtime.PROJECT.mkdir(parents=True, exist_ok=True)
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)
    db.db_init()

    # Start background workers.
    # Each worker consumes tasks from the shared queue and processes them.
    runtime.shutdown_event.clear()
    runtime.active_workers.clear()

    # Recover tasks left unfinished by a previous process.
    task_queue.recover_queued_tasks()

    # Remove stale temporary files from previous runs.
    ingest.cleanup_old_temp_files()

    # Trim the play journal, then settle up with Navidrome: deliver whatever
    # was queued while it was unreachable, and look for playlists it is still
    # showing after their .m3u went away. Neither is allowed to stop startup --
    # the service has to come up with Navidrome switched off.
    try:
        removed = db.prune_play_history(config.PLAY_HISTORY_DAYS)
        if removed:
            logger.info("Pruned %d play journal entries", removed, extra={"task_id": "system"})
    except Exception as exc:
        logger.warning("Could not prune the play journal: %s", exc, extra={"task_id": "system"})

    if navidrome.configured():
        try:
            navidrome.drain()
            navidrome.reconcile()
        except Exception as exc:
            logger.info("Navidrome not reachable at startup: %s", exc, extra={"task_id": "system"})

    for i in range(config.MAX_WORKERS):
        worker_thread = threading.Thread(
            target=task_queue.worker,
            name=f"music-adder-worker-{i + 1}",
            daemon=True,
        )
        runtime.active_workers.append(worker_thread)
        worker_thread.start()

    logger.info(
        f"Started {len(runtime.active_workers)} worker(s)",
        extra={"task_id": "system"},
    )

    yield

    # Uvicorn handles SIGTERM and enters the lifespan shutdown phase.
    # Stop workers without blocking indefinitely.
    runtime.shutdown_event.set()

    if runtime.active_workers:
        deadline = time.monotonic() + config.SHUTDOWN_TIMEOUT
        for worker_thread in runtime.active_workers:
            remaining = max(0, deadline - time.monotonic())
            worker_thread.join(timeout=remaining)

        runtime.active_workers.clear()

    # Cleanup temporary files after workers have stopped.
    if runtime.TMP_DIR.exists():
        for f in runtime.TMP_DIR.glob("*"):
            try:
                f.unlink()
                logger.info(
                    f"Cleaned up temp file: {f.name}",
                    extra={"task_id": "system"},
                )
            except OSError as e:
                logger.warning(
                    f"Could not remove temp file during shutdown {f.name}: {e}",
                    extra={"task_id": "system"},
                )


app = FastAPI(lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(runtime.PROJECT.parent / "web")),
    name="static",
)


class AddRequest(BaseModel):
    links: list[str]


class DeleteRequest(BaseModel):
    path: str


class PlaylistCreateRequest(BaseModel):
    name: str
    paths: list[str] = []


class PlaylistRenameRequest(BaseModel):
    name: str


class PlaylistTracksRequest(BaseModel):
    """A whole new order in one request.

    Sending the complete list rather than a move instruction is what makes the
    write atomic: the file is replaced in one go, so a reader never sees a
    half-applied reorder. ``revision`` is the hash handed out by the matching
    GET, and it is what stops an edit made on the laptop from silently
    overwriting one just made on the phone.
    """

    paths: list[str]
    revision: str | None = None


class PlayRequest(BaseModel):
    path: str
    played_seconds: float
    duration: float | None = None
    skipped: bool = False
    source: str = "player"


@app.post("/api/add")
def add(req: AddRequest, authenticated: bool = Depends(verify_token)):
    """Add YouTube links to queue (Problem #19: API auth)."""
    # Problem #9: Check request limits
    if len(req.links) > config.MAX_LINKS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many links. Maximum {config.MAX_LINKS_PER_REQUEST} per request.",
        )

    # Problem #9: Check queue size limit
    current_queue_size = runtime.TASK_QUEUE.qsize()
    if current_queue_size + len(req.links) > config.MAX_QUEUE_SIZE:
        raise HTTPException(
            status_code=429,
            detail=f"Queue full. Current: {current_queue_size}, Max: {config.MAX_QUEUE_SIZE}",
        )

    ids = []
    for link in req.links:
        link = link.strip()
        if not link:
            continue

        # Problem #10: Validate URL
        is_valid, error_msg = ingest.validate_url(link)
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Invalid URL: {error_msg}")

        # Normalize all supported YouTube URL forms to one canonical URL
        # before duplicate checks and database insertion.
        try:
            link = ingest.canonicalize_youtube_url(link)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid YouTube URL: {exc}",
            ) from exc

        # Problem #7: Check for duplicate URLs already in queue/processing
        with runtime.FILE_LOCK:
            if link in runtime.PROCESSING_URLS:
                continue  # Skip duplicate

            # Check if URL already exists in database.
            # Failed tasks can be explicitly retried by re-submitting the URL.
            existing = db.db_query(
                "SELECT id, status FROM tasks WHERE url = ?",
                (link,),
            )

            if existing:
                task = existing[0]

                if task["status"] != "error":
                    continue  # Skip active/completed duplicate

                # Reuse the existing failed task instead of inserting a
                # second row, which would violate the UNIQUE(url) index.
                tid = task["id"]
                db.task_update(
                    tid,
                    status="queued",
                    artist=None,
                    title=None,
                    error=None,
                    error_type=None,
                    retry_count=0,
                )
            else:
                cur = db.db_exec(
                    "INSERT INTO tasks(url, status) VALUES(?, 'queued')",
                    (link,),
                )
                tid = cur.lastrowid

            runtime.TASK_QUEUE.put((tid, link))
            runtime.PROCESSING_URLS.add(link)
            ids.append(tid)

    return {"added": ids}


@app.get("/api/tasks")
def tasks(authenticated: bool = Depends(verify_token)):
    return db.db_query("SELECT * FROM tasks ORDER BY id DESC LIMIT 50")


@app.get("/api/library")
def list_library(q: str = "", limit: int = 200, authenticated: bool = Depends(verify_token)):
    """List library tracks, optionally filtered by a substring of artist/title/album."""
    needle = q.strip().lower()
    out = []
    for row in library.library_index():
        if needle and needle not in row["haystack"]:
            continue
        out.append({k: row[k] for k in ("path", "artist", "title", "album", "track", "duration")})
        if len(out) >= limit:
            break
    return out


@app.delete("/api/library")
def delete_track(req: DeleteRequest, authenticated: bool = Depends(verify_token)):
    """Remove a track from the library, moving it to trash rather than unlinking."""
    return library.delete_track(req.path)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


@app.get("/api/stream-url")
def get_stream_url(path: str, authenticated: bool = Depends(verify_token)):
    """Mint a short-lived playable URL for one track.

    Issued when playback is about to start rather than when a list is rendered,
    so the lifetime is spent on the track rather than on the browsing that
    preceded it.
    """
    track = library.library_track(path)
    duration = None
    for row in library.library_index():
        if row["path"] == path:
            duration = row.get("duration")
            break
    if duration is None:
        from mutagen.mp4 import MP4

        with suppress(Exception):
            duration = MP4(track).info.length
    return signing.stream_url(path, duration)


@app.get("/api/stream")
def stream(path: str, exp: str = "", sig: str = ""):
    """Serve one track to an <audio> element.

    Deliberately outside verify_token: an <audio> element sends no headers, and
    the signature is what stands in for the bearer token here. A missing or
    stale signature is refused; it is not a way around the token.
    """
    if not signing.verify(path, exp, sig):
        raise HTTPException(status_code=403, detail="Stream link is invalid or has expired")
    track = library.library_track(path)
    # FileResponse handles Range itself, which is what makes seeking work:
    # starlette parses the header, answers 206, and returns 416 on a bad range.
    return FileResponse(track, media_type="audio/mp4", filename=track.name)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


@app.get("/api/playlists")
def get_playlists(authenticated: bool = Depends(verify_token)):
    return playlists.listing()


@app.post("/api/playlists")
def create_playlist(req: PlaylistCreateRequest, authenticated: bool = Depends(verify_token)):
    playlist = playlists.create(req.name, req.paths)
    return _playlist_payload(playlist)


@app.get("/api/playlists/{name}/tracks")
def get_playlist_tracks(name: str, authenticated: bool = Depends(verify_token)):
    return _playlist_payload(playlists.read(name))


@app.put("/api/playlists/{name}/tracks")
def put_playlist_tracks(
    name: str,
    req: PlaylistTracksRequest,
    authenticated: bool = Depends(verify_token),
):
    """Replace the order and contents of a playlist."""
    playlist = playlists.write(name, req.paths, req.revision)
    return _playlist_payload(playlist)


@app.patch("/api/playlists/{name}")
def rename_playlist(
    name: str,
    req: PlaylistRenameRequest,
    authenticated: bool = Depends(verify_token),
):
    """Rename a playlist, and clean up after Navidrome.

    Navidrome does not follow a rename: it adds the new file as a second
    playlist and keeps the first one, cover and all. So the old one is deleted
    through its API and the cover is re-uploaded onto the new one.
    """
    playlist = playlists.rename(name, req.name)
    covers.rename(name, req.name)
    _sync_navidrome("delete", name)
    if covers.cover_file(req.name) is not None:
        _sync_navidrome("cover", req.name)
    return _playlist_payload(playlist)


@app.delete("/api/playlists/{name}")
def delete_playlist(name: str, authenticated: bool = Depends(verify_token)):
    """Delete a playlist here and in Navidrome.

    Removing the file is not enough: Navidrome keeps showing a playlist whose
    .m3u is gone, so without the second half this leaves a corpse behind.
    """
    result = playlists.delete(name)
    covers.delete(name)
    result["navidrome"] = _sync_navidrome("delete", name)
    return result


def _playlist_payload(playlist: playlists.Playlist) -> dict:
    return {
        "name": playlist.name,
        "revision": playlist.revision,
        "cover": covers.media_type(playlist.name) is not None,
        "entries": [
            {
                "index": entry.index,
                "path": entry.path,
                "title": entry.title,
                "duration": entry.duration,
            }
            for entry in playlist.entries
        ],
    }


def _sync_navidrome(op: str, name: str) -> str:
    """Do the Navidrome half now, or queue it for when Navidrome is back."""
    if not navidrome.configured():
        return "not configured"
    try:
        if op == "delete":
            navidrome.delete_playlist(name)
        elif op == "cover":
            image = covers.read_cover(name)
            if image is not None:
                navidrome.upload_cover(name, image[0], image[1])
        return "done"
    except Exception as exc:
        logger.warning("Navidrome %s for %r deferred: %s", op, name, exc)
        navidrome.enqueue(op, name)
        return "queued"


@app.post("/api/playlists/{name}/cover")
async def upload_playlist_cover(
    name: str,
    image: UploadFile = File(...),
    authenticated: bool = Depends(verify_token),
):
    """Give a playlist its own picture.

    Stored locally first and replicated to Navidrome second. Navidrome is where
    a Subsonic client reads the cover from, but it ties one to a playlist id,
    and an id is born from a file -- rename the .m3u and the cover stays with
    the playlist that no longer exists. Keeping the original here makes that a
    re-upload rather than a loss, and lets the phone see the new cover straight
    away instead of waiting for a round trip through Navidrome.
    """
    playlists.read(name)  # 404 for a playlist that does not exist
    data = await image.read()
    media = covers.store(name, data)
    return {
        "playlist": name,
        "media_type": media,
        "bytes": len(data),
        "navidrome": _sync_navidrome("cover", name),
    }


@app.get("/api/playlists/{name}/cover")
def get_playlist_cover(name: str, authenticated: bool = Depends(verify_token)):
    """Serve the stored cover, so the panel can show it without Navidrome."""
    image = covers.read_cover(name)
    if image is None:
        raise HTTPException(status_code=404, detail="No cover for this playlist")
    return Response(content=image[0], media_type=covers.media_type(name))


@app.delete("/api/playlists/{name}/cover")
def delete_playlist_cover(name: str, authenticated: bool = Depends(verify_token)):
    covers.delete(name)
    return {"playlist": name, "cover": "removed"}


# ---------------------------------------------------------------------------
# Play journal
# ---------------------------------------------------------------------------

_PLAYS_WINDOW: dict[str, list[float]] = {}
_PLAYS_LOCK = threading.Lock()
PLAYS_PER_MINUTE = 60


@app.post("/api/plays")
def record_play(req: PlayRequest, authenticated: bool = Depends(verify_token)):
    """Record one finished or abandoned track.

    Navidrome stores a play count and the date of the last play, not a log, so
    "what was playing at this hour" cannot be asked of it. This is where that
    question gets its data -- which also means it only ever sees the laptop,
    since the phone plays through Amperfy.
    """
    now = time.monotonic()
    with _PLAYS_LOCK:
        window = [t for t in _PLAYS_WINDOW.get("all", []) if now - t < 60]
        if len(window) >= PLAYS_PER_MINUTE:
            raise HTTPException(
                status_code=429,
                detail="Too many play events; a played track cannot arrive that often",
            )
        window.append(now)
        _PLAYS_WINDOW["all"] = window

    db.db_exec(
        "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, source) "
        "VALUES(?, datetime('now','localtime'), ?, ?, ?, ?)",
        (req.path, req.played_seconds, req.duration, 1 if req.skipped else 0, req.source),
    )
    return {"recorded": req.path}


@app.get("/api/plays/stats")
def play_stats(authenticated: bool = Depends(verify_token)):
    """Skip rate and journal size -- the numbers acceptance criterion 26 needs."""
    rows = db.db_query(
        "SELECT COUNT(*) AS total, SUM(skipped) AS skipped, "
        "COUNT(DISTINCT path) AS distinct_tracks FROM plays"
    )[0]
    total = rows["total"] or 0
    skipped = rows["skipped"] or 0
    return {
        "plays": total,
        "skipped": skipped,
        "skip_rate": round(skipped / total, 4) if total else None,
        "distinct_tracks": rows["distinct_tracks"] or 0,
    }


@app.get("/health")
def health():
    """Health endpoint (Problem #21)."""
    try:
        # Check database connectivity
        db.db_exec("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {str(e)[:100]}"

    # Check library path
    library_status = "ok" if config.LIBRARY.exists() else f"not found: {config.LIBRARY}"

    # Queue stats
    queue_size = runtime.TASK_QUEUE.qsize()

    healthy = db_status == "ok" and library_status == "ok"
    tracks, albums = library.library_counts() if library_status == "ok" else (0, 0)

    payload = {
        "status": "healthy" if healthy else "unhealthy",
        "database": db_status,
        "library": library_status,
        "library_path": str(config.LIBRARY),
        "workers": config.MAX_WORKERS,
        "queue_size": queue_size,
        "max_queue_size": config.MAX_QUEUE_SIZE,
        "tracks": tracks,
        "albums": albums,
        "playlists": len(playlists.listing()) if library_status == "ok" else 0,
        "navidrome": "configured" if navidrome.configured() else "not configured",
        "navidrome_pending": navidrome.pending_count(),
        "plays_logged": (
            db.db_query("SELECT COUNT(*) AS n FROM plays")[0]["n"] if db_status == "ok" else 0
        ),
    }

    if not healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload,
        )

    return payload


@app.get("/", response_class=HTMLResponse)
def index():
    return (runtime.PROJECT.parent / "web" / "index.html").read_text(encoding="utf-8")
