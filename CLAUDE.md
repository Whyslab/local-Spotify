# CLAUDE.md

## What this is

`local-Spotify` is a self-hosted music library for one household. A YouTube link (or a Spotify
playlist, a search, an uploaded file) goes in; a tagged audio file with cover art comes out in a
folder that [Navidrome](https://www.navidrome.org/) serves to phones over the Subsonic API. The
same service also has its own web player with playlists, lyrics and "smart" shuffles, plus a
desktop window around that page (`desktop/`). It runs as a systemd user unit on a home Linux
machine, on a trusted LAN.

Ingest flow: `POST /api/add` → SQLite task + in-memory queue → worker thread (`queue.process`) →
`ingest.download_to_temp` (`yt-dlp -J`, then `yt-dlp -x --audio-format m4a` into `adder/tmp/`,
integrity check) → `ingest.ingest_temp_file` (Deezer lookup via `enrich`, cover Deezer → iTunes →
YouTube thumbnail, tags, content-hash duplicate check) → atomic move to
`<LIBRARY_PATH>/<Artist>/Singles/<Title>.<ext>`. A file never reaches the library before every
check has passed.

## Stack

- Python 3.12+, FastAPI + uvicorn, worker threads, SQLite (`adder/adder.db`)
- yt-dlp (+ `yt-dlp-ejs`) as a subprocess; needs **ffmpeg/ffprobe** and **Deno** on `PATH`
- mutagen for tags (m4a, mp3, flac, opus); `requests`, `httpx`, `urllib` for HTTP
- Optional librosa (`scripts/requirements-analysis.txt`) for tempo/key/energy analysis
- Vanilla JS UI in `web/` (`app.js` panel, `player.js` player), served by the same app
- systemd user unit (`deploy/`); Navidrome as a separate system service
- pytest, ruff; CI in `.github/workflows/ci.yml` (installs ffmpeg)

## Layout

- `adder/app.py` — HTTP surface only; each concern lives in its own module
- `adder/runtime.py` — process-wide state: paths (`TMP_DIR`, `DB_PATH`, `TRASH_DIR`, …), logging,
  `TASK_QUEUE`, `PROCESSING_URLS`, `shutdown_event`
- `adder/config.py` — reads `adder/.env`; validates at import time (refuses an empty or
  placeholder `API_TOKEN`)
- `adder/queue.py` — retry policy, workers, recovery after restart
- `adder/ingest.py` — naming, yt-dlp calls, `classify_error`, tagging, filing into the library
- `adder/enrich.py` — Deezer metadata; every failure returns `None`, never raises
- `adder/library.py` — library index, path resolution, deletion to `trash/`
- `adder/playlists.py`, `sync.py`, `navidrome.py`, `covers.py` — `.m3u` playlists and their
  two-way sync with Navidrome
- `adder/shuffle.py`, `outside.py`, `similar.py`, `moods.py`, `shelves.py`, `analysis.py` —
  shuffles, home shelves, tracks from outside the library, audio analysis scheduling
- `adder/lyrics.py`, `signing.py` (signed stream URLs), `sources.py` (search, playlist import)
- `scripts/`, `adder/fix_covers.py` — offline tools; `scripts/README.md` describes them
- `tests/` — offline pytest suite; `tests/fixtures/tone.m4a` is a real 1-second AAC file
- `AUDIT.md` — known problems by priority and their status

## Commands

```bash
# setup
python3 -m venv .venv
.venv/bin/pip install -r adder/requirements.txt ruff
cp .env.example adder/.env    # then set API_TOKEN

# run
.venv/bin/python -m adder.server              # http://0.0.0.0:8787

# test and lint — exactly what CI runs (import/streaming tests need ffmpeg)
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check adder scripts tests desktop
.venv/bin/python -m compileall -q adder scripts tests desktop

# production
./deploy/install.sh
journalctl --user -u music-adder -f
```

## Conventions and gotchas

- **Tests must stay offline.** `tests/conftest.py` stubs Deezer, lyrics, analysis and outside
  downloads for every test, and fails any test that opens a non-loopback socket (or connects to
  an HTTP proxy). Network helpers swallow errors on purpose, so an unmocked call would otherwise
  pass silently. To test a stubbed function for real, capture it at module import
  (`real = enrich.lookup`) before the fixture replaces it.
- Paths live on `runtime` and config values on `config`, and modules read them at call time:
  in tests patch `runtime.TMP_DIR`, `config.LIBRARY`, `config.MAX_RETRIES`, … — never a copy.
- `runtime.TASK_QUEUE`, `PROCESSING_URLS` and `shutdown_event` are shared across tests; drain or
  reset them in tests that touch workers.
- Errors: `ingest.classify_error()` maps a message to an `error_type`; only
  `ingest.RETRYABLE_ERRORS` are retried. yt-dlp failures carry its last `ERROR:` line
  (`ingest.ytdlp_error()`). New yt-dlp wordings go into `classify_error` with a test in
  `tests/test_error_classification.py`.
- Anything from YouTube or tags is untrusted: filenames go through `ingest.sanitize_filename`,
  and the frontend renders it with `textContent` only (an XSS regression test enforces this).
- The systemd unit uses `ProtectSystem=strict` + `ProtectHome=read-only`: the service may write
  only to `adder/`, `trash/` and `LIBRARY_PATH`. A new writable path must live there or be added
  to `deploy/music-adder.service.template`.
- Log with `extra={"task_id": ...}` (`"system"` outside a task).
- Keep `ruff format` clean; CI fails on formatting before it runs the tests.
- `README.md` and `README.ru.md` are kept in sync line for line.
