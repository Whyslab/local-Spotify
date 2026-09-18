"""YouTube -> Navidrome: HTTP surface. Everything else lives in its own module.

This file is deliberately thin: it wires FastAPI to the pieces and does no work
of its own. The library lives in :mod:`adder.library`, the processing pipeline
in :mod:`adder.ingest`, the queue and its retry policy in :mod:`adder.queue`.
"""

import hashlib
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    config,
    covers,
    db,
    ingest,
    library,
    lyrics,
    moods,
    navidrome,
    playlists,
    runtime,
    shelves,
    shuffle,
    signing,
    sources,
    sync,
)
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

    # Watch for playlist edits made anywhere but here. Navidrome accepts a
    # reorder from a phone into its own database and never writes it to the
    # .m3u, so the edit lives only until the next time this service touches
    # that file. See adder/sync.py.
    sync.start()

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
    sync.stop()

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
    # Which kind of queue this track came out of. Criterion 26 is a comparison
    # of skip rates between the two, and that comparison needs the label at the
    # moment the track is played -- it cannot be reconstructed afterwards.
    mode: str = "manual"


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
def list_library(
    q: str = "",
    limit: int = 200,
    sort: str = "new",
    authenticated: bool = Depends(verify_token),
):
    """List library tracks, optionally filtered by a substring of artist/title/album.

    ``sort=new`` ставит наверх недавно добавленное. Так список отвечает на
    вопрос, с которым его чаще всего и открывают: что приехало и куда это
    разложить. ``sort=name`` возвращает прежний порядок — по пути на диске,
    то есть по артисту.
    """
    needle = q.strip().lower()
    rows = library.library_index()
    if sort == "new":
        rows = sorted(rows, key=lambda r: r.get("added") or 0, reverse=True)
    out = []
    for row in rows:
        if needle and needle not in row["haystack"]:
            continue
        # albumartist нужен для группировки по альбомам: у трека с фитом
        # artist — это "A • B", а альбом всё равно принадлежит одному.
        out.append(
            {
                k: row[k]
                for k in (
                    "path",
                    "artist",
                    "albumartist",
                    "title",
                    "album",
                    "track",
                    "duration",
                    "added",
                )
            }
        )
        if len(out) >= limit:
            break
    return out


@app.get("/api/track")
def track_details(path: str, authenticated: bool = Depends(verify_token)):
    """Everything known about one track, for the panel beside the list.

    Tags come from the library index, the three measured numbers from the
    analysis table. A track that has not been measured yet simply has none of
    them -- the panel leaves that half blank rather than showing a zero, which
    would read as "this track has no tempo" instead of "nobody has looked".
    """
    # library_track answers with an absolute path and, more to the point,
    # refuses anything that escapes the library. The index is keyed on the
    # relative form, so take it back from the resolved file rather than
    # trusting the string that arrived.
    absolute = library.library_track(path)
    relative = str(absolute.relative_to(config.LIBRARY.resolve()))
    row = next((r for r in library.library_index() if r["path"] == relative), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Track not found")
    measured = db.db_query(
        "SELECT tempo, energy, brightness, music_key, mode FROM audio_features WHERE path = ?",
        (relative,),
    )
    # haystack is the lowercased blob the library search matches against. It is
    # an implementation detail of that search and three times the size of
    # everything else here.
    return {
        **{key: value for key, value in row.items() if key != "haystack"},
        "features": measured[0] if measured else None,
    }


@app.get("/api/lyrics")
def track_lyrics(path: str, authenticated: bool = Depends(verify_token)):
    """Текст играющего трека, по возможности с таймингами.

    Спрашивается один раз на трек и кладётся в кэш на диск: каталог текстов
    бесплатный и чужой, дёргать его при каждом воспроизведении незачем.
    Промах тоже запоминается, но на две недели — текст может появиться позже.
    """
    return lyrics.for_track(path)


@app.get("/api/cover")
def track_cover(path: str, authenticated: bool = Depends(verify_token)):
    """The artwork inside a track, for the panel beside the list.

    Served from the file rather than from Navidrome: the page already has a
    token for this service and would need a second one for that, and the
    picture is in the file either way. Cached hard -- the bytes only change
    when the file is retagged, and then its path is the same but the panel is
    re-rendered anyway.
    """
    absolute = library.library_track(path)
    art = library.embedded_cover(absolute)
    if art is None:
        raise HTTPException(status_code=404, detail="This track has no artwork")
    return Response(
        content=art[0],
        media_type=art[1],
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.delete("/api/library")
def delete_track(req: DeleteRequest, authenticated: bool = Depends(verify_token)):
    """Remove a track from the library, moving it to trash rather than unlinking."""
    return library.delete_track(req.path)


# ---------------------------------------------------------------------------
# Getting music in
# ---------------------------------------------------------------------------


def _queue_source(source_key: str) -> int | None:
    """Put one source key in the queue, or skip it if it is already there.

    Same rules as a pasted link: an active or finished task is left alone, a
    failed one is reset and tried again.
    """
    with runtime.FILE_LOCK:
        if source_key in runtime.PROCESSING_URLS:
            return None
        existing = db.db_query("SELECT id, status FROM tasks WHERE url = ?", (source_key,))
        if existing:
            task = existing[0]
            if task["status"] != "error":
                return None
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
            cur = db.db_exec("INSERT INTO tasks(url, status) VALUES(?, 'queued')", (source_key,))
            tid = cur.lastrowid
        runtime.TASK_QUEUE.put((tid, source_key))
        runtime.PROCESSING_URLS.add(source_key)
        return tid


@app.post("/api/import")
async def import_files(
    files: list[UploadFile] = File(...),
    authenticated: bool = Depends(verify_token),
):
    """Take audio files from the machine and put them through the same pipeline.

    Nothing is re-encoded: a file keeps the container it arrived in. Turning an
    mp3 into an m4a to make the folder uniform would cost a generation of
    quality for tidiness, and Navidrome serves all of these already.
    """
    if len(files) > config.MAX_LINKS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {config.MAX_LINKS_PER_REQUEST} per request.",
        )

    accepted, skipped = [], []
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in library.AUDIO_SUFFIXES:
            skipped.append(
                {
                    "file": upload.filename,
                    "reason": f"unsupported format {suffix or '(none)'}",
                }
            )
            continue
        data = await upload.read()
        if not data:
            skipped.append({"file": upload.filename, "reason": "empty file"})
            continue
        source_key, _ = ingest.stash_upload(data, upload.filename or "track" + suffix)
        tid = _queue_source(source_key)
        if tid is None:
            skipped.append({"file": upload.filename, "reason": "already in the library or queued"})
        else:
            accepted.append({"file": upload.filename, "task": tid})

    return {"accepted": accepted, "skipped": skipped}


class ReplaceRequest(BaseModel):
    """Какой трек меняем и на что. Ссылка — для замены с YouTube."""

    path: str
    url: str


@app.post("/api/replace")
def replace_track(req: ReplaceRequest, authenticated: bool = Depends(verify_token)):
    """Заменить трек фонотеки другой его версией с YouTube.

    Бывает, что скачалась не та запись: концертник вместо студийной, дорожка
    из клипа вместо трека. Меняем не байты файла, а то, на что смотрят
    подборки: новая версия проходит обычный конвейер со своими тегами, а когда
    доедет — встаёт на место старой, сохраняя её место в списке. Старая уезжает
    в корзину.

    Всё это происходит после загрузки, в рабочем потоке: страницу можно закрыть.
    """
    # Путь проверяется сразу: замену несуществующего трека лучше отвергнуть
    # здесь, чем узнать об этом через минуту в журнале.
    library.library_track(req.path)

    is_valid, error_msg = ingest.validate_url(req.url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Invalid URL: {error_msg}")

    try:
        link = ingest.canonicalize_youtube_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YouTube URL: {exc}") from exc

    tid = _queue_source(link)
    if tid is None:
        raise HTTPException(
            status_code=409,
            detail="Эта ссылка уже в очереди или её трек уже в фонотеке",
        )
    db.task_update(tid, replace_of=req.path)
    return {"task": tid}


@app.post("/api/replace-file")
async def replace_track_with_file(
    path: str,
    file: UploadFile = File(...),
    authenticated: bool = Depends(verify_token),
):
    """То же самое, но правильная версия приходит файлом с диска."""
    library.library_track(path)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in library.AUDIO_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Формат {suffix or '(нет)'} не поддерживается")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл")

    source_key, _ = ingest.stash_upload(data, file.filename or "track" + suffix)
    tid = _queue_source(source_key)
    if tid is None:
        raise HTTPException(status_code=409, detail="Этот файл уже в очереди или уже в фонотеке")
    db.task_update(tid, replace_of=path)
    return {"task": tid}


@app.get("/api/search")
def search(q: str, limit: int = 8, authenticated: bool = Depends(verify_token)):
    """Look for a track on YouTube without downloading anything.

    The results are shown so a person can choose between them. Two uploads of
    the same song differ in length and in channel, and picking automatically is
    what filled the library with live versions the last time it was tried.
    """
    try:
        return {"results": sources.search_youtube(q, limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc


class PlaylistImportRequest(BaseModel):
    url: str


@app.post("/api/import-playlist")
def import_playlist(req: PlaylistImportRequest, authenticated: bool = Depends(verify_token)):
    """Queue a whole playlist from one link, YouTube or Spotify.

    YouTube is direct: the links are already the thing to download. Spotify is
    not -- it names tracks, and each one has to be found on YouTube first.
    A track is only accepted when the artist, the title and the length all
    agree; anything else is reported rather than guessed at, because guessing
    is what produced a shelf of live takes and other people's covers.
    """
    url = (req.url or "").strip()

    if sources.spotify_playlist_id(url):
        try:
            candidates, truncated = sources.spotify_playlist(url)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc

        queued, unmatched = [], []
        for candidate in candidates:
            match = sources.best_youtube_match(candidate)
            if match is None:
                unmatched.append({"artist": candidate.artist, "title": candidate.title})
                continue
            tid = _queue_source(ingest.canonicalize_youtube_url(match["url"]))
            if tid is not None:
                queued.append(tid)

        return {
            "source": "spotify",
            "read": len(candidates),
            "queued": len(queued),
            "unmatched": unmatched,
            # The embed page carries no total, so this cannot be "100 of N" --
            # only "this is as far as the source goes".
            "truncated": truncated,
            "note": (
                f"Spotify отдаёт не больше {sources.SPOTIFY_EMBED_LIMIT} треков по ссылке "
                "и не сообщает, сколько их всего. Полный список — импортом из CSV."
                if truncated
                else ""
            ),
        }

    is_valid, error_msg = ingest.validate_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Invalid URL: {error_msg}")
    try:
        video_urls = sources.youtube_playlist(url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc

    queued = []
    for video_url in video_urls:
        tid = _queue_source(video_url)
        if tid is not None:
            queued.append(tid)
    return {
        "source": "youtube",
        "read": len(video_urls),
        "queued": len(queued),
        "unmatched": [],
        "truncated": False,
        "note": "",
    }


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
# Two-way sync with the phone
# ---------------------------------------------------------------------------


@app.get("/api/sync")
def sync_status(authenticated: bool = Depends(verify_token)):
    """What the background pull-back loop last found.

    The panel shows this so that "the phone and the laptop agree" is something
    you can look at rather than hope for.
    """
    return sync.status()


@app.post("/api/sync")
def sync_now(apply: bool = True, authenticated: bool = Depends(verify_token)):
    """Run a pass now instead of waiting for the timer.

    ``apply=false`` reports what it would rewrite and rewrites nothing.
    """
    try:
        return sync.check(apply=apply)
    except navidrome.NavidromeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Shuffling
# ---------------------------------------------------------------------------


def _shuffle_tracks() -> list[shuffle.Track]:
    """Everything the queue builder needs, out of the three tables that hold it."""
    features = db.db_query("SELECT path, tempo, energy, brightness, music_key FROM audio_features")
    plays = db.db_query(
        "SELECT path, CAST(strftime('%H', played_at) AS INTEGER) AS hour "
        "FROM plays ORDER BY id DESC LIMIT 2000"
    )
    return shuffle.tracks_from_rows(
        library.library_index(), features, plays, int(time.strftime("%H"))
    )


def _queue_payload(queue: list[shuffle.Track], core: set[str] | None = None) -> list[dict]:
    by_path = {row["path"]: row for row in library.library_index()}
    out = []
    for track in queue:
        row = by_path.get(track.path, {})
        entry = {
            "path": track.path,
            "artist": row.get("artist") or track.artist,
            "title": row.get("title") or track.path,
            "duration": row.get("duration"),
            "tempo": track.tempo,
        }
        if core is not None:
            # Чтобы в очереди было видно, что пришло со стороны, а не гадать.
            entry["outside"] = track.path not in core
        out.append(entry)
    return out


@app.get("/api/home")
def home(authenticated: bool = Depends(verify_token)):
    """Главная: подборки по настроению, находки и несколько альбомов.

    Всё считается из того, что уже есть на диске. Единственный выход наружу —
    список похожих артистов, и он кэшируется навсегда: соседство артистов
    меняется годами, а не днями.
    """
    rows = library.library_index()
    features = db.db_query("SELECT path, tempo, energy, brightness FROM audio_features")

    by_path = {row["path"]: row for row in rows}

    def as_tracks(paths: list[str]) -> list[dict]:
        out = []
        for path in paths:
            row = by_path.get(path)
            if row:
                out.append({
                    k: row.get(k)
                    for k in ("path", "artist", "title", "album", "duration")
                })
        return out

    albums: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        who = (row.get("albumartist") or row.get("artist") or "").strip()
        name = (row.get("album") or "").strip()
        if who and name:
            albums.setdefault((who, name), []).append(row)
    big = sorted((k for k, v in albums.items() if len(v) > 1),
                 key=lambda k: -len(albums[k]))[:12]

    return {
        "moods": [
            {"key": m.key, "name": m.name, "hint": m.hint, "tracks": as_tracks(m.paths)}
            for m in moods.collections(features, limit=40)
        ],
        "discover": shelves.discover(rows),
        "albums": [
            {
                "artist": who, "album": name, "count": len(albums[(who, name)]),
                "cover": albums[(who, name)][0]["path"],
            }
            for who, name in big
        ],
    }


@app.get("/api/discover-external")
def discover_external(limit: int = 12, authenticated: bool = Depends(verify_token)):
    """Находки извне: треки похожих артистов, которых в фонотеке нет.

    Ничего не скачивает и ничего не ставит в очередь — только называет. Дальше
    человек открывает поиск и выбирает версию сам, как и в /api/search.

    Пустой список — нормальный ответ: Deezer мог не отозваться, а находки
    к очереди пристроены сбоку. Отдавать 502 значило бы объявить сбоем то, что
    им не является.
    """
    return {"tracks": shelves.external(library.library_index(), want=limit)}


@app.get("/api/shuffle")
def smart_shuffle(
    size: int = 50,
    mode: str = "smart",
    playlist: str = "",
    authenticated: bool = Depends(verify_token),
):
    """Build a queue: neighbours that follow each other, and the shelf covered.

    ``mode=plain`` is uniform random, kept as the thing the smart one is
    measured against rather than as a feature.

    ``playlist`` — перемешать подборку, не забывая про остальную фонотеку:
    подборка остаётся костяком очереди, и примерно треть мест достаётся
    подходящим трекам со стороны. Без него перемешивается вся фонотека, как
    раньше.
    """
    tracks = _shuffle_tracks()
    if not tracks:
        return {"mode": mode, "queue": [], "report": {}, "analysed": 0}

    core: set[str] | None = None
    if playlist:
        # read() сам отвечает 404, если подборки нет.
        core = {entry.path for entry in playlists.read(playlist).entries}
        known = {track.path for track in tracks}
        core &= known

    if mode == "plain":
        # Ровно то, с чем сравнивают: равномерно и только из подборки, если её
        # назвали. Подмешивать сюда сторону нельзя — иначе сравнение сломано.
        pool = [t for t in tracks if t.path in core] if core else tracks
        queue = shuffle.plain_shuffle(pool, size=size)
    else:
        queue = shuffle.build_queue(tracks, size=size, core=core)

    analysed = sum(1 for track in tracks if track.tempo)
    body = {
        "mode": mode,
        "queue": _queue_payload(queue, core if mode == "smart" else None),
        "report": shuffle.queue_report(queue),
        "analysed": analysed,
        "total": len(tracks),
    }
    if core is not None:
        body["playlist"] = playlist
        body["core"] = len(core)
        body["outside"] = sum(1 for track in queue if track.path not in core)
    return body


@app.post("/api/shuffle/blind")
def start_blind_trial(size: int = 30, authenticated: bool = Depends(verify_token)):
    """Two queues, one of each kind, without saying which is which.

    The only honest way to answer "is it actually better". Which side got the
    smart queue is recorded here and not returned, so the answer cannot be
    read off the response.
    """
    tracks = _shuffle_tracks()
    if len(tracks) < 4:
        raise HTTPException(status_code=400, detail="Not enough tracks to compare")

    smart = shuffle.build_queue(tracks, size=size)
    plain = shuffle.plain_shuffle(tracks, size=size)
    smart_side = secrets.choice(["A", "B"])

    cur = db.db_exec("INSERT INTO blind_trials(smart_side) VALUES(?)", (smart_side,))
    return {
        "trial": cur.lastrowid,
        "A": _queue_payload(smart if smart_side == "A" else plain),
        "B": _queue_payload(plain if smart_side == "A" else smart),
    }


class BlindChoiceRequest(BaseModel):
    choice: str


@app.post("/api/shuffle/blind/{trial_id}")
def finish_blind_trial(
    trial_id: int,
    req: BlindChoiceRequest,
    authenticated: bool = Depends(verify_token),
):
    choice = (req.choice or "").strip().upper()
    if choice not in {"A", "B"}:
        raise HTTPException(status_code=400, detail="Choice must be A or B")

    rows = db.db_query("SELECT smart_side, choice FROM blind_trials WHERE id = ?", (trial_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="No such trial")
    if rows[0]["choice"]:
        raise HTTPException(status_code=409, detail="This trial already has an answer")

    db.db_exec(
        "UPDATE blind_trials SET choice = ?, decided_at = datetime('now','localtime') WHERE id = ?",
        (choice, trial_id),
    )
    return {"trial": trial_id, "recorded": choice}


@app.get("/api/shuffle/blind/results")
def blind_results(authenticated: bool = Depends(verify_token)):
    """How the comparison stands. Acceptance is 7 of 10 or better."""
    rows = db.db_query("SELECT smart_side, choice FROM blind_trials WHERE choice IS NOT NULL")
    decided = len(rows)
    smart_chosen = sum(1 for row in rows if row["choice"] == row["smart_side"])
    return {
        "decided": decided,
        "smart_chosen": smart_chosen,
        "share": round(smart_chosen / decided, 3) if decided else None,
        "passes": decided >= 10 and smart_chosen / decided >= 0.7,
    }


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
        "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, source, mode) "
        "VALUES(?, datetime('now','localtime'), ?, ?, ?, ?, ?)",
        (
            req.path,
            req.played_seconds,
            req.duration,
            1 if req.skipped else 0,
            req.source,
            req.mode,
        ),
    )
    return {"recorded": req.path}


@app.get("/api/plays/stats")
def play_stats(authenticated: bool = Depends(verify_token)):
    """Skip rate overall and per queue kind -- the numbers criterion 26 is stated in.

    ``ready`` says whether there is enough of a journal to read anything into
    the difference: 200 plays on each side, or a fortnight. Below that the two
    numbers are noise, and reporting them as a result would be worse than
    reporting nothing.
    """
    overall = db.db_query(
        "SELECT COUNT(*) AS total, SUM(skipped) AS skipped, "
        "COUNT(DISTINCT path) AS distinct_tracks FROM plays"
    )[0]
    total = overall["total"] or 0
    skipped = overall["skipped"] or 0

    by_mode = {}
    for row in db.db_query(
        "SELECT mode, COUNT(*) AS total, SUM(skipped) AS skipped FROM plays GROUP BY mode"
    ):
        mode_total = row["total"] or 0
        mode_skipped = row["skipped"] or 0
        by_mode[row["mode"] or "manual"] = {
            "plays": mode_total,
            "skipped": mode_skipped,
            "skip_rate": round(mode_skipped / mode_total, 4) if mode_total else None,
        }

    smart, plain = by_mode.get("smart", {}), by_mode.get("plain", {})
    ready = min(smart.get("plays", 0), plain.get("plays", 0)) >= 200
    difference = None
    if smart.get("skip_rate") is not None and plain.get("skip_rate") is not None:
        difference = round(plain["skip_rate"] - smart["skip_rate"], 4)

    return {
        "plays": total,
        "skipped": skipped,
        "skip_rate": round(skipped / total, 4) if total else None,
        "distinct_tracks": overall["distinct_tracks"] or 0,
        "by_mode": by_mode,
        "comparison": {
            "ready": ready,
            "difference": difference,
            # Criterion 26: five percentage points, on one listener, so
            # indicative rather than significant.
            "passes": bool(ready and difference is not None and difference >= 0.05),
        },
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
        # Что в работе прямо сейчас, а не только что ждёт своей очереди:
        # queue_size выше — это длина очереди, и пока качается единственный трек,
        # она равна нулю. Панель показывает этот список в рельсе, поэтому он
        # приходит вместе с остальным здоровьем, а не отдельным опросом каждые
        # три секунды.
        #
        # Ошибки попадают сюда за последний час: старая ошибка остаётся в
        # таблице навсегда, и без срока она висела бы в рельсе вечно.
        "active_tasks": (
            db.db_query(
                "SELECT artist, title, url, status FROM tasks "
                "WHERE status IN ('queued', 'downloading', 'tagging') "
                "   OR (status = 'error' "
                "       AND updated_at > datetime('now', 'localtime', '-1 hour')) "
                "   OR (status = 'done' "
                "       AND updated_at > datetime('now', 'localtime', '-2 minutes')) "
                "ORDER BY id DESC LIMIT 10"
            )
            if db_status == "ok"
            else []
        ),
        "sync_last_check_seconds_ago": sync.status()["last_check_seconds_ago"],
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
    """Страница вместе с версиями файлов, которые она подключает.

    Без этого браузер держит style.css в кэше и не спрашивает сервер, свежий
    ли он. Выходит худшее из двух: новый app.js рисует то, чего старый css не
    умеет разложить, — обложки без размеров разворачиваются во весь экран, и
    выглядит это как сломанная вёрстка, а не как устаревший кэш.

    К каждой ссылке дописывается отпечаток самого файла. Файл не менялся —
    адрес тот же, и браузер честно берёт своё из кэша; изменился — адрес
    другой, и старому кэшу нечего подставить.
    """
    web = runtime.PROJECT.parent / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js", "player.js"):
        path = web / name
        if not path.is_file():
            continue
        stamp = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={stamp}")
    return html
