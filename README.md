# 🎵 local-Spotify

*[Русская версия](README.ru.md)*

[![CI](https://github.com/Whyslab/local-Spotify/actions/workflows/ci.yml/badge.svg)](https://github.com/Whyslab/local-Spotify/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](adder/requirements.txt)

**A self-hosted music library: drop in a YouTube link, get back a tagged track with cover art in a personal library you can stream to your phone through Navidrome.**

`local-Spotify` is a small home-network service. It pulls audio from YouTube, normalises it into a consistent shape — clean filenames, ID3/MP4 tags, HD cover art — and files it into a library served by [Navidrome](https://www.navidrome.org/) over the Subsonic API. On a phone it behaves like your own Spotify: [Amperfy](https://github.com/BLL-Games/Amperfy), play\:Sub, DSub and any other Subsonic client connect to it as they would to a commercial streaming service.

It is not built to be a public SaaS or to work around YouTube's restrictions — it is a tool for one person or household on a trusted network.

---

## Contents

* [Features](#-features)
* [Architecture](#️-architecture)
* [Quick start](#-quick-start)
* [Configuration](#️-configuration)
* [API](#-api)
* [Production: systemd](#-production-systemd)
* [Tests](#-tests)
* [Security](#-security)
* [Project layout](#-project-layout)
* [Limitations](#️-limitations)
* [Roadmap](#️-roadmap)
* [Licence](#-licence)

---

## ✨ Features

* **Add music by link** — POST a list of YouTube URLs; the service handles the rest.
* **Background queue with multiple workers** — downloads run in parallel (`MAX_WORKERS`) and never block the API.
* **Automatic metadata cleanup** — `Song (Official Video) [4K]` becomes a clean `Artist / Song`, while genuine variants like `(Live)` or `(Remix)` are preserved in the tags.
* **HD cover art** — iTunes Search API with a fallback to the YouTube thumbnail; a separate script (`fix_covers.py`) backfills missing artwork afterwards via iTunes → Deezer.
* **Content-based deduplication** — each track is hashed (SHA-256) and compared against what is already in the library, rather than matched on filename.
* **Retry with exponential backoff** — transient network and download failures are retried automatically; permanent ones are not.
* **Graceful shutdown and recovery** — `SIGTERM` stops workers cleanly, including child `yt-dlp`/`ffmpeg` processes, and unfinished tasks survive a service restart.
* **File integrity checks** — every M4A is validated before and after tags are written, so corrupt files never reach the library.
* **Resource limits** — caps on queue size, links per request, and free disk space required before a download starts.
* **Bearer-token auth on the API**; the health check needs no authorisation.
* **Web interface** — a minimal single-page UI for adding links and watching the queue (`web/`).
* **Library audit tooling** — offline scripts for finding duplicates, checking metadata, and bulk-migrating a playlist from CSV.

---

## 🏗️ Architecture

```text
                    ┌──────────────┐
                    │   iPhone /    │
                    │   Android     │
                    │   (Amperfy)   │
                    └──────┬───────┘
                           │ Subsonic API
                           ▼
                    ┌──────────────┐        reads files
                    │  Navidrome   │───────────────────────┐
                    └──────────────┘                       │
                                                             ▼
┌──────────┐   POST /api/add   ┌──────────────┐   ┌──────────────────┐
│  client  │ ─────────────────▶│  adder API   │   │ Normalized Library│
│ (curl/UI)│                   │  (FastAPI)   │   │  Artist/Singles/  │
└──────────┘                   └──────┬───────┘   └────────▲──────────┘
                                       │ task queue                 │
                                       ▼                             │
                              ┌──────────────────┐                   │
                              │  N worker threads │───────────────────┘
                              │  yt-dlp → ffmpeg  │   validate, then write
                              │  → mutagen (tags) │   only once checks pass
                              └──────────────────┘
```

The governing principle: a file never lands in the library directly. Downloading and processing happen in a temporary directory (`adder/tmp/`), and only after metadata, integrity and duplicate checks all pass is the file moved atomically into `Normalized Library`.

---

## 🚀 Quick start

Requires Linux, Python 3.12+, [FFmpeg](https://ffmpeg.org/) and git.

```bash
# 1. Clone
git clone https://github.com/Whyslab/local-Spotify.git
cd local-Spotify

# 2. Virtualenv and dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r adder/requirements.txt

# 3. Configure
cp .env.example adder/.env
python -c 'import secrets; print(secrets.token_urlsafe(32))'   # paste into API_TOKEN
$EDITOR adder/.env

# 4. Run
python -m adder.server
```

The service listens on `http://0.0.0.0:8787`. Check it:

```bash
curl http://127.0.0.1:8787/health
```

```bash
TOKEN=$(grep '^API_TOKEN=' adder/.env | cut -d= -f2-)

curl -X POST http://127.0.0.1:8787/api/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"links": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]}'
```

The web interface is served at the service root (`/`), where you can enter the token and watch the queue.

---

## ⚙️ Configuration

Everything is read from `adder/.env` (see `.env.example`). The service refuses to start without a valid `API_TOKEN` — a deliberate choice, since the API is reachable from the whole local network.

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_TOKEN` | *(required)* | Bearer token for API access |
| `LIBRARY_PATH` | `~/Music/Normalized Library` | Where the music library lives |
| `PORT` / `HOST` | `8787` / `0.0.0.0` | Listen address |
| `MAX_WORKERS` | `2` | Parallel download workers |
| `MAX_LINKS_PER_REQUEST` | `100` | Link cap for one `/api/add` call |
| `MAX_QUEUE_SIZE` | `5000` | Maximum queued tasks |
| `PRESERVE_FEAT_ARTISTS` | `true` | Keep `feat./ft.` in the artist directory name |
| `MAX_RETRIES` | `3` | Attempts per task on transient errors |
| `RETRY_BACKOFF_BASE` | `2.0` | Exponential backoff base, in seconds |
| `SHUTDOWN_TIMEOUT` | `30` | Graceful shutdown timeout, in seconds |
| `MIN_FREE_SPACE_MB` | `2048` | Free disk space required before downloading |
| `TMP_TTL_HOURS` | `24` | Age at which stranded temp files are cleaned up |

---

## 🔌 API

Authorise with an `Authorization: Bearer <API_TOKEN>` header. `/health` needs no authorisation.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Status of the service, database, library and queue |
| `POST` | `/api/add` | Add one or more YouTube links |
| `GET` | `/api/tasks` | The 50 most recent tasks and their status |
| `GET` | `/` | Web interface |

<details>
<summary><code>POST /api/add</code> — example</summary>

```bash
curl -X POST http://127.0.0.1:8787/api/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "links": [
      "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "https://youtu.be/anotherVideoId"
    ]
  }'
```

```json
{ "added": [12, 13] }
```

Only links to `youtube.com`, `m.youtube.com`, `music.youtube.com` and `youtu.be` are accepted. Different URL forms pointing at the same video are canonicalised, so they do not create duplicate tasks. Re-submitting a link whose task has already completed or is still running is ignored; a link whose task ended in `error` can be re-submitted to queue it again.

</details>

<details>
<summary><code>GET /health</code> — example response</summary>

```json
{
  "status": "healthy",
  "database": "ok",
  "library": "ok",
  "library_path": "/home/user/Music/Normalized Library",
  "workers": 2,
  "queue_size": 0,
  "max_queue_size": 5000
}
```

If the database is unreachable or the library directory is missing, the status becomes `unhealthy` and the response code becomes `503`.

</details>

---

## 🖥 Production: systemd

For continuous background operation the service runs as a systemd user unit. The install script checks that `.venv` exists and `API_TOKEN` is filled in, generates the unit, and optionally configures Navidrome and `ufw` rules for the LAN:

```bash
./deploy/install.sh
```

```bash
systemctl --user status music-adder
journalctl --user -u music-adder -f
```

The unit runs with `WorkingDirectory` at the repository root and permits writes only to `adder/` (database and temp files) and the library path — `ProtectSystem=strict` prevents the process from writing anywhere else, including the source tree and `.git`.

Back up state (SQLite plus `.env`):

```bash
./deploy/backup.sh
```

---

## 🧪 Tests

```bash
PYTHONPATH="$PWD" pytest -q
```

96 tests cover API authorisation, YouTube link validation and canonicalisation, content-based deduplication, error classification and which failures are worth retrying, the retry logic and how it interacts with graceful shutdown, task recovery after a restart, temp-file cleanup, track title cleaning, and an XSS regression in the frontend — asserting that data from untrusted sources (YouTube video metadata) never reaches the DOM through `innerHTML`.

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `compileall` and the full suite on a clean environment for every push and pull request.

---

## 🔒 Security

* The API is protected by a Bearer token compared with `secrets.compare_digest` (timing-attack resistant); the service will not start without one.
* Data from YouTube (video title, uploader) is treated as untrusted: the frontend renders it only through `textContent`/`replaceChildren`, never `innerHTML`.
* Only YouTube URLs with an exact host match are accepted, which blocks bypasses of the `youtube.com.evil.example` variety.
* The systemd unit sets `ProtectSystem=strict`, `NoNewPrivileges` and `PrivateTmp`, and permits writes only to `adder/` and the library path.
* The token and `.env` are never committed (`.gitignore`). Do not paste a real `API_TOKEN` into a README or an issue.

Found a vulnerability? Please open a private security advisory on the repository rather than a public issue.

---

## 📁 Project layout

```text
local-Spotify/
├── adder/                  # Ingest and processing service
│   ├── app.py              # FastAPI app, workers, business logic
│   ├── config.py           # Loads and validates configuration from .env
│   ├── server.py           # Entry point (uvicorn)
│   ├── fix_covers.py       # Offline backfill for missing cover art
│   └── requirements.txt
├── web/                    # Static web interface (vanilla JS)
├── scripts/                # Offline tools: library audit, duplicate finder, playlist migration
├── tests/                  # pytest, 96 tests
├── deploy/                 # systemd unit, install/backup scripts, Navidrome config
└── .env.example
```

---

## ⚠️ Limitations

This is a self-hosted home project. It is not intended for:

* a public SaaS or high-load production deployment;
* bulk or commercial use;
* circumventing YouTube's regional or other restrictions.

Before downloading third-party content, make sure you have the right to do so.

---

## 🗺️ Roadmap

* [ ] Deleting and reorganising tracks through the API
* [ ] Importing whole albums and playlists, not just individual links
* [ ] A Docker image, for deployment without systemd
* [ ] Prometheus metrics on top of the current `/health`

---

## 📜 Licence

[MIT](LICENSE). Make sure you have the right to download and store any third-party content you add to the library.

---

<p align="center">
  <a href="https://github.com/Whyslab">Whyslab</a> ·
  <a href="https://github.com/Whyslab/local-Spotify">local-Spotify</a>
</p>
