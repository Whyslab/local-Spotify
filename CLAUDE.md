# CLAUDE.md

## What this is

`local-Spotify` is a self-hosted service for one household: a YouTube link goes in, a tagged M4A
with cover art comes out in a folder that [Navidrome](https://www.navidrome.org/) serves to phones
over the Subsonic API. It runs as a systemd user unit on a home Linux machine, on a trusted LAN.

Flow: `POST /api/add` → SQLite task + in-memory queue → worker thread → `yt-dlp -J` (metadata) →
`yt-dlp -x --audio-format m4a` into `adder/tmp/` → Deezer lookup (album, track number, artists) →
cover (Deezer → iTunes → YouTube thumbnail) → mutagen writes tags → integrity + content-hash
duplicate check → atomic move to `<LIBRARY_PATH>/<Artist>/Singles/<Title>.m4a`. A file never
reaches the library before every check has passed.

## Stack

- Python 3.12+, FastAPI + uvicorn, worker threads (`threading`), SQLite (`adder/adder.db`)
- yt-dlp (+ `yt-dlp-ejs`) run as a subprocess; needs **ffmpeg/ffprobe** and **Deno** on `PATH`
- mutagen for MP4 tags; `requests` (covers) and `urllib` (Deezer, in `enrich.py`)
- Vanilla JS single-page UI in `web/`, served by the same app
- systemd user unit (`deploy/`), Navidrome as a separate system service
- pytest, ruff; CI in `.github/workflows/ci.yml`

## Layout

- `adder/app.py` — everything at runtime: API, auth, queue, workers, yt-dlp calls, error
  classification and retries, tagging, library index and deletion
- `adder/enrich.py` — Deezer metadata lookup; every failure returns `None`, never raises
- `adder/config.py` — reads `adder/.env`; validates at import time (refuses an empty or placeholder
  `API_TOKEN`)
- `adder/server.py` — entry point (`python -m adder.server`)
- `adder/fix_covers.py`, `scripts/` — offline maintenance tools, not used by the service
- `deploy/` — `install.sh` (systemd unit + Navidrome config), `backup.sh`, unit template
- `tests/` — offline pytest suite; `tests/fixtures/tone.m4a` is a real 1-second AAC file
- `AUDIT.md` — known problems by priority; medium/low items are open

## Commands

```bash
# setup
python3 -m venv .venv
.venv/bin/pip install -r adder/requirements.txt ruff
cp .env.example adder/.env    # then set API_TOKEN

# run
.venv/bin/python -m adder.server              # http://0.0.0.0:8787, /health needs no token

# test and lint — exactly what CI runs
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check adder scripts tests
.venv/bin/python -m compileall -q adder scripts tests

# production
./deploy/install.sh
journalctl --user -u music-adder -f
```

## Conventions and gotchas

- **Tests must stay offline.** `tests/conftest.py` fails any test that opens a network socket.
  Mock `yt_meta` / `yt_download` / `run_yt_dlp`, `enrich.lookup` or `enrich._get`,
  `fetch_cover` / `fetch_cover_url`. Network helpers swallow errors on purpose, so an unmocked call
  would otherwise pass silently.
- `config.py` values are imported into `app.py` by name at import time. In tests, patch
  `adder.app.<NAME>` (e.g. `LIBRARY`, `TMP_DIR`, `DB_PATH`, `MAX_RETRIES`), not `config`.
- `TASK_QUEUE`, `PROCESSING_URLS` and `shutdown_event` are module globals shared across tests;
  drain or reset them in tests that touch workers.
- Error handling: `classify_error()` maps a message to an `error_type`; only `RETRYABLE_ERRORS`
  are retried. yt-dlp failures carry its last `ERROR:` line (`ytdlp_error()`). Add new yt-dlp
  wordings to `classify_error` with a test in `tests/test_error_classification.py`.
- Anything taken from YouTube metadata is untrusted: filenames go through `sanitize_filename`,
  and the frontend renders it with `textContent` only (an XSS regression test enforces this).
- The systemd unit uses `ProtectSystem=strict`: at runtime the service may write only to
  `adder/` (DB, `tmp/`, `trash/`) and `LIBRARY_PATH`. New writable paths must live there or be
  added to `deploy/music-adder.service.template`.
- Always log with `extra={"task_id": ...}` (`"system"` outside a task).
- Keep `ruff format` clean; CI fails on formatting before it runs the tests.
- `README.md` and `README.ru.md` are kept in sync line for line.
