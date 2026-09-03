"""YouTube -> Navidrome: HTTP surface. Everything else lives in its own module.

This file is deliberately thin: it wires FastAPI to the pieces and does no work
of its own. The library lives in :mod:`adder.library`, the processing pipeline
in :mod:`adder.ingest`, the queue and its retry policy in :mod:`adder.queue`.
"""

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, ingest, library, runtime
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
