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
* [Troubleshooting](#-troubleshooting)
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
* **Bearer-token auth on the API**; without a token the health check answers only whether the service is up.
* **Web interface** — a minimal single-page UI for adding links and watching the queue (`web/`).
* **Playlists as files** — every playlist is an `.m3u` in the library root, edited by rewriting it; Navidrome re-reads it within about six seconds.
* **An edit made on the phone is not lost** — Navidrome accepts a reorder sent by a client but never writes the `.m3u`, and its own watcher discards that edit the next time the file changes. Every thirty seconds the service looks for a playlist whose database was edited past its file and writes the order into the file. Measured: the edit reaches the `.m3u` in ten seconds.
* **Reordering that survives duplicates** — a line is identified by its index, not its path. `Monday.m3u` holds nineteen tracks that appear twice.
* **Concurrent edits are refused, not merged** — every read hands out a hash of the file and every write must present it; a stale edit gets `409` instead of quietly overwriting one made from the phone.
* **Playlist covers** — stored locally and replicated to Navidrome, so a rename is a re-upload rather than a loss. The type is decided by the bytes, not by the file name.
* **A player** — the same page in two widths, with playback served through short-lived signed URLs, so an `<audio>` element can seek without the API token ever entering a URL.
* **Play journal** — Navidrome keeps a play count and a last-played date, not a log. This one records track, time, how far it got and whether it was skipped.
* **Shuffling that has a shape** — the library has no genre or BPM tags at all, so `scripts/analyze_audio.py` measures tempo, energy, brightness and key from the audio itself, and queues are walked rather than sampled: neighbours are close in tempo, the same artist does not follow itself, and a track heard recently is unlikely to come back. See `scripts/README.md`.
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

Tested on Debian/Ubuntu. Every step ends with a command that proves it worked — do not move on until it does.

### 1. System packages

| Needed | Why | Check |
| --- | --- | --- |
| Linux with systemd | the production unit (step 7) | `systemctl --user status` |
| Python **3.12+** with `venv` | the service | `python3 --version` |
| **FFmpeg** (with `ffprobe`) | yt-dlp extracts the audio with it; without it every download fails | `ffmpeg -version && ffprobe -version` |
| **Deno** | yt-dlp needs a JavaScript runtime to solve YouTube's player challenges; Deno is the one it uses by default | `deno --version` |
| git | cloning | `git --version` |

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg git curl unzip libnotify-bin

# Deno into /usr/local/bin, so the systemd service finds it on its default PATH.
# (The official installer's default, ~/.deno/bin, is on your shell's PATH but not the service's.)
curl -fsSLo /tmp/deno.zip \
  "https://github.com/denoland/deno/releases/latest/download/deno-$(uname -m)-unknown-linux-gnu.zip"
sudo unzip -o /tmp/deno.zip -d /usr/local/bin
deno --version
```

On Ubuntu 22.04 and older `python3` is 3.10/3.11: install 3.12 (for example from the deadsnakes PPA as `python3.12` + `python3.12-venv`) and use `python3.12` instead of `python3` below.

### 2. Clone

```bash
git clone https://github.com/Whyslab/local-Spotify.git
cd local-Spotify
```

All commands below are run from this directory.

### 3. Virtualenv and dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r adder/requirements.txt
.venv/bin/python -m yt_dlp --version     # prints a version, e.g. 2026.08.19
```

Optional: the smart shuffle measures tempo, key and energy of each track with librosa, which is kept out of the main requirements because it pulls in a compiler toolchain. Without it everything else works:

```bash
.venv/bin/pip install -r scripts/requirements-analysis.txt
```

The venv must be at `.venv` in the repository root: `deploy/install.sh` and the systemd unit look for it there.

### 4. Configure

```bash
cp .env.example adder/.env
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
sed -i "s|^API_TOKEN=.*|API_TOKEN=$TOKEN|" adder/.env
grep '^API_TOKEN=' adder/.env             # must NOT be CHANGE_ME_...
```

That is the only required setting. The service refuses to start with an empty token or with the placeholder from `.env.example`.

Optional, in `adder/.env`:

* `LIBRARY_PATH` — where the music goes. Default: `~/Music/Normalized Library`. To change it, uncomment the line and give an absolute path **without quotes**. Navidrome must read the same folder (step 6).
* `COOKIES_FROM_BROWSER` — only if YouTube starts answering "Sign in to confirm you're not a bot" (see [Troubleshooting](#-troubleshooting)).

The full list is under [Configuration](#️-configuration).

### 5. Run and check

```bash
.venv/bin/python -m adder.server
```

The service listens on `http://0.0.0.0:8787`. In a second terminal:

```bash
TOKEN=$(grep '^API_TOKEN=' adder/.env | cut -d= -f2-)
curl -s http://127.0.0.1:8787/health -H "Authorization: Bearer $TOKEN"
```

Without the token `/health` answers only `{"status": ...}`. Expect `"status":"healthy"`, `"ffmpeg":"ok"` and `"js_runtime":"ok"`. A `503` with `"ffmpeg":"missing"` means step 1 is incomplete; the startup log says the same.

Add a track:

```bash
curl -X POST http://127.0.0.1:8787/api/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"links": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]}'
# {"added":[1]}

curl -s http://127.0.0.1:8787/api/tasks -H "Authorization: Bearer $TOKEN"
# status goes queued -> downloading -> tagging -> done
```

The file appears as `<LIBRARY_PATH>/<Artist>/Singles/<Title>.m4a`. The log line `Task finished: stored` marks it; on failure the log has an `ERROR` line with the cause and `/api/tasks` has it in `error` / `error_type`.

The web interface is at `http://<host>:8787/`: paste the token once, then add links and watch the queue from a phone.

Stop the service with `Ctrl+C` before step 7.

### 6. Navidrome

`local-Spotify` only fills a folder; [Navidrome](https://www.navidrome.org/) streams it. Install it by following [the official Linux guide](https://www.navidrome.org/docs/installation/linux/) (a `.deb`/`.rpm` package or the release tarball; the service must be called `navidrome`).

Then point Navidrome's `MusicFolder` at your `LIBRARY_PATH`. Step 7 does this for you if Navidrome has no config yet: it writes `/etc/navidrome/navidrome.toml` from `deploy/navidrome.toml.example` and adds `deploy/navidrome-override.conf`, which lets the Navidrome service read a folder under `/home`. An existing config is never overwritten — the script prints the `MusicFolder` line to check instead.

For playlist covers, and to remove a playlist from Navidrome when its `.m3u` is deleted, also set `NAVIDROME_USER` and `NAVIDROME_PASSWORD` (a Navidrome user) in `adder/.env`. Without them the service works and queues that part until they appear.

Check: open `http://<host>:4533`, create the admin user, and the track from step 5 shows up after the scan (Navidrome watches the folder; a full rescan is under *Settings → Scan*).

### 7. Run permanently (systemd)

```bash
./deploy/install.sh
systemctl --user status music-adder       # active (running)
curl -s http://127.0.0.1:8787/health     # {"status":"healthy"}
```

See [Production: systemd](#-production-systemd) for what the script does. A desktop window with media keys around the same page is described in [`desktop/README.md`](desktop/README.md).

---

## ⚙️ Configuration

Everything is read from `adder/.env` (see `.env.example`); real environment variables take precedence. The service refuses to start without a valid `API_TOKEN` — a deliberate choice, since the API is reachable from the whole local network.

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_TOKEN` | *(required)* | Bearer token for API access. Empty or the `.env.example` placeholder is rejected |
| `LIBRARY_PATH` | `~/Music/Normalized Library` | Where the music library lives; must match Navidrome's `MusicFolder`. Absolute path, no quotes |
| `PORT` / `HOST` | `8787` / `0.0.0.0` | Listen address |
| `MAX_WORKERS` | `2` | Parallel download workers |
| `MAX_LINKS_PER_REQUEST` | `100` | Link cap for one `/api/add` call |
| `MAX_QUEUE_SIZE` | `5000` | Maximum queued tasks |
| `PRESERVE_FEAT_ARTISTS` | `true` | Keep `feat./ft.` in the artist directory name |
| `MAX_RETRIES` | `3` | Attempts per task on transient errors (network, HTTP 429) |
| `RETRY_BACKOFF_BASE` | `2.0` | Exponential backoff base, in seconds |
| `RATE_LIMIT_BACKOFF` | `60` | After YouTube rate-limits (HTTP 429), seconds to wait per attempt; no yt-dlp call starts meanwhile |
| `MAX_DURATION_MINUTES` | `30` | Longest video accepted, in minutes; `0` for no limit. Live streams are always refused |
| `DESKTOP_NOTIFICATIONS` | `true` | Pop-up notifications on this machine: tracks added (batched), downloads failed, a yt-dlp update rolled back. Needs `notify-send` |
| `SHUTDOWN_TIMEOUT` | `30` | Graceful shutdown timeout, in seconds |
| `MIN_FREE_SPACE_MB` | `2048` | Free disk space required before downloading |
| `TMP_TTL_HOURS` | `24` | Age at which stranded temp files are cleaned up |
| `COOKIES_FROM_BROWSER` | *(empty)* | yt-dlp `--cookies-from-browser` value: `firefox`, `chrome`, or `firefox:/path/to/profile` |
| `NAVIDROME_URL` | `http://127.0.0.1:4533` | Navidrome, for playlist covers and deleting playlists there |
| `NAVIDROME_USER` / `NAVIDROME_PASSWORD` | *(empty)* | A Navidrome user; optional, that work is queued until they are set |
| `MAX_COVER_BYTES` | `8388608` | Largest accepted playlist cover upload |
| `PLAY_HISTORY_DAYS` | `400` | How long the play journal is kept |
| `DELAY_BETWEEN_TRACKS` | `1.1` | Pause between tracks in the offline scripts, not the service |

Changes take effect after a restart (`systemctl --user restart music-adder`).

---

## 🔌 API

Authorise with an `Authorization: Bearer <API_TOKEN>` header. `/health` without it returns only `{"status": ...}`; the full answer below needs the token.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Status of the service, database, library, ffmpeg, JS runtime and queue |
| `POST` | `/api/add` | Add one or more YouTube links |
| `GET` | `/api/tasks` | The 50 most recent tasks and their status |
| `POST` | `/api/tasks/retry-failed` | Queue every failed task again (also a button in the panel) |
| `GET` | `/` | Web interface |
| `GET` | `/api/playlists` | List playlists |
| `POST` | `/api/playlists` | Create a playlist |
| `PATCH` | `/api/playlists/{name}` | Rename a playlist |
| `DELETE` | `/api/playlists/{name}` | Delete a playlist (file to `trash/`, and removed from Navidrome) |
| `GET` | `/api/playlists/{name}/tracks` | A playlist's tracks, with the revision needed to edit it |
| `PUT` | `/api/playlists/{name}/tracks` | Replace order and contents; `409` if the revision is stale |
| `POST` `GET` `DELETE` | `/api/playlists/{name}/cover` | Upload, fetch or remove a playlist cover |
| `GET` | `/api/stream-url` | Mint a short-lived signed URL for one track |
| `GET` | `/api/stream` | Serve a track to `<audio>`; authorised by that signature, not the token |
| `POST` | `/api/plays` | Record a finished or abandoned track |
| `GET` | `/api/plays/stats` | Skip rate overall and per queue kind |
| `POST` | `/api/import` | Take audio files from disk through the same pipeline |
| `GET` | `/api/search` | Look for a track on YouTube without downloading |
| `POST` | `/api/import-playlist` | Queue a whole playlist from one link, YouTube or Spotify |
| `GET` | `/api/shuffle` | Build a queue (`mode=smart` or `plain`) |
| `POST` | `/api/shuffle/blind` | Two queues, one of each kind, unlabelled |
| `POST` | `/api/shuffle/blind/{id}` | Record which one was preferred |
| `GET` | `/api/shuffle/blind/results` | How the comparison stands |
| `GET` | `/api/sync` | What the last synchronisation pass found |
| `POST` | `/api/sync` | Run a pass now; `?apply=false` reports only |
| `GET` | `/api/track` | A track's tags plus its tempo, key and energy |
| `GET` | `/api/cover` | The artwork inside the file; `?size=96\|300\|600` for a cached thumbnail |
| `GET` `DELETE` | `/api/library` | The library (search, sort, paging) / move a track to `trash/` |
| `POST` | `/api/replace` `/api/replace-file` | Replace a track by a better version, keeping its place in every playlist |
| `GET` | `/api/home` | Home page shelves |
| `GET` | `/api/discover-external` | Tracks by similar artists that are not in the library |
| `POST` | `/api/shuffle/smart` | Smart shuffle of any set of tracks (album, current queue); about a third are new tracks |
| `GET` `POST` | `/api/outside/status` `/api/outside/prefetch` | State of new (non-library) tracks; download ahead |
| `POST` | `/api/outside/{key}/keep` | Put a new track into the library through the normal import |
| `GET` | `/api/lyrics` | Lyrics, with timings where available |
| `GET` `POST` | `/api/lyrics/candidates` `/api/lyrics/choose` `/api/lyrics/custom` | Pick lyrics by hand or paste your own |

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

Only links to `youtube.com`, `m.youtube.com`, `music.youtube.com` and `youtu.be` are accepted. Shorts, live and embed links (`/shorts/ID`, `/live/ID`, `/embed/ID`) are accepted too. Different URL forms pointing at the same video are canonicalised, so they do not create duplicate tasks. Re-submitting a link whose task has already completed or is still running is ignored; a link whose task ended in `error` can be re-submitted to queue it again.

</details>

<details>
<summary><code>GET /api/tasks</code> — task statuses and error types</summary>

`status` is one of `queued`, `downloading`, `tagging`, `done`, `error`. For `error`, `error` holds yt-dlp's own error line and `error_type` says what to do:

| `error_type` | Retried automatically | Meaning |
| --- | --- | --- |
| `network_error`, `download_error`, `artwork_error` | yes | Transient failure |
| `rate_limited` | yes, and again by itself an hour later (up to 3 times) | YouTube is throttling (HTTP 429 / "try again later") |
| `youtube_not_found` | no | Video removed, private or blocked in your region |
| `unsupported_video` | no | A live stream, or longer than `MAX_DURATION_MINUTES` |
| `youtube_auth_required` | no | Bot check, age gate, members-only: set `COOKIES_FROM_BROWSER` |
| `dependency_error` | no | ffmpeg/ffprobe missing on this machine |
| `invalid_url`, `filesystem_error`, `database_error`, `metadata_error`, `internal_error`, `unknown_error` | no | See the service log for the task id |

A failed link can be re-submitted once the cause is fixed.

</details>

<details>
<summary><code>GET /health</code> — example response</summary>

```json
{
  "status": "healthy",
  "database": "ok",
  "library": "ok",
  "library_path": "/home/user/Music/Normalized Library",
  "ffmpeg": "ok",
  "js_runtime": "ok",
  "workers": 2,
  "queue_size": 0,
  "max_queue_size": 5000
}
```

If the database is unreachable, the library directory is missing or ffmpeg/ffprobe are not installed, the status becomes `unhealthy` and the response code `503`. A missing Deno shows as `"js_runtime": "missing"` but does not make the service unhealthy.

</details>

---

## 🖥 Production: systemd

For continuous background operation the service runs as a systemd user unit. The install script checks that `.venv` exists and `API_TOKEN` is filled in, generates the unit, and optionally configures Navidrome and `ufw` rules for the LAN:

```bash
./deploy/install.sh
```

The script refuses to continue without `.venv` or with an unset/placeholder `API_TOKEN`; writes the unit for `LIBRARY_PATH` and enables lingering; installs three timers — nightly audio analysis, nightly shelf export, and a weekly yt-dlp update that rolls itself back if the new version cannot read YouTube; if `navidrome` is installed and has no config yet, writes `/etc/navidrome/navidrome.toml` with the same `MusicFolder` (an existing config is left alone); and, if `ufw` is active, opens Navidrome and the service to the LAN subnet (`LAN_SUBNET=192.168.1.0/24` to choose it).

```bash
systemctl --user status music-adder
journalctl --user -u music-adder -f      # one line per task: Processing / Task finished / ERROR
systemctl --user restart music-adder     # after editing adder/.env
```

Updating:

```bash
git pull
.venv/bin/pip install -r adder/requirements.txt
./deploy/install.sh                      # the unit changes between versions
```

The unit runs with `WorkingDirectory` at the repository root and permits writes only to `adder/` (database and temp files), `trash/` (deleted tracks) and the library path. `ProtectSystem=strict` alone is not enough for that: it mounts `/` read-only but leaves `/home` writable, since `/home` is a separate mount point. `ProtectHome=read-only` closes it, and the paths above are carved back out with `ReadWritePaths`.

Back up state (SQLite plus `.env`):

```bash
./deploy/backup.sh      # to ~/local-spotify-backups, keeps the last 10
```

---

## 🧪 Tests

```bash
.venv/bin/pip install -r requirements-dev.txt   # pytest and ruff
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check adder scripts tests desktop
```

The suite runs fully offline and needs no real `.env`; the import and streaming tests need `ffmpeg`, which CI installs. `tests/conftest.py` fails any test that opens a network connection, so YouTube, Deezer and iTunes are always mocked. It covers:

* **downloading** — the yt-dlp commands, reading its JSON and its `ERROR:` line, the subprocess runner (timeouts, shutdown, large output), and which failures are retried;
* **metadata** — splitting YouTube titles into artist and title, and Deezer enrichment against canned API responses;
* **writing into the library** — `process()` end to end on a real one-second AAC file (`tests/fixtures/tone.m4a`): tags read back with mutagen, the `Artist/Singles/Title.m4a` layout, cover fallback, corrupt downloads, content deduplication;
* the API: authorisation, link validation and canonicalisation, deletion and path traversal, `/health`, task recovery after a restart, graceful shutdown, and an XSS regression in the frontend;
* playlists, Navidrome synchronisation, streaming signatures, file import, lyrics, shuffles and the smart-shuffle downloads.

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `compileall` and the full suite on a clean environment for every push and pull request.

---

## 🛠 Troubleshooting

Start with `curl -s http://127.0.0.1:8787/health -H "Authorization: Bearer $TOKEN"` and `journalctl --user -u music-adder -n 50`.

| Symptom | Cause and fix |
| --- | --- |
| `RuntimeError: API_TOKEN is required` at startup | Token empty or still the placeholder. Redo [step 4](#4-configure). |
| `PermissionError` creating the library at startup | `LIBRARY_PATH` points somewhere you cannot write. Fix it in `adder/.env` or comment it out for the default. |
| `/health` → `"ffmpeg": "missing"`, tasks fail with `dependency_error` | `sudo apt install ffmpeg`, then restart the service. |
| `/health` → `"js_runtime": "missing"`, log warns about Deno | Install Deno into `/usr/local/bin` as in [step 1](#1-system-packages). A Deno under `~/.deno/bin` works in your shell but not in the systemd service. |
| `youtube_auth_required`: "Sign in to confirm you're not a bot" | YouTube distrusts this IP. Log in to YouTube in a browser on the same machine and set `COOKIES_FROM_BROWSER=firefox` (or `chrome`, or `firefox:/path/to/profile`) in `adder/.env`, restart, re-submit the link. |
| Many `rate_limited` errors | YouTube is throttling; the service already pauses all downloads for `RATE_LIMIT_BACKOFF` seconds per attempt. If it persists, lower `MAX_WORKERS` to `1`, wait an hour and re-submit the failed links. |
| Downloads that used to work start failing | YouTube changed something. yt-dlp updates itself weekly; to update now run `.venv/bin/python scripts/update_ytdlp.py` (no restart needed). `journalctl --user -u music-ytdlp-update` shows past runs. |
| Task is `done` but Navidrome doesn't show the track | Navidrome reads another folder: its `MusicFolder` (`/etc/navidrome/navidrome.toml`) must equal `library_path` from `/health`. Check that the Navidrome user can read it: `sudo -u navidrome ls "<library_path>"`. Then *Settings → Scan* in Navidrome. |
| Deleting a track from the web interface fails | Deleted files go to `trash/` in the repository root. After `git pull`, re-run `./deploy/install.sh` so the unit has `ReadWritePaths` for it. |
| Web interface says the token is wrong | Paste the value of `API_TOKEN` from `adder/.env`, without `API_TOKEN=`. |

---

## 🔒 Security

* The API is protected by a Bearer token compared with `secrets.compare_digest` (timing-attack resistant); the service will not start without one.
* Data from YouTube (video title, uploader) is treated as untrusted: the frontend renders it only through `textContent`/`replaceChildren`, never `innerHTML`.
* Only YouTube URLs with an exact host match are accepted, which blocks bypasses of the `youtube.com.evil.example` variety.
* The systemd unit sets `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges` and `PrivateTmp`, and permits writes only to `adder/`, `trash/` and the library path. `ProtectHome` is what actually confines the process: `ProtectSystem=strict` does not cover `/home`, so without it the service could write to its own source tree.
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
├── tests/                  # pytest
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

One thing about the phone. Amperfy 2.1 **does not show a playlist's own cover**: its response parser does not read the `coverArt` field at all and draws a collage from the first tracks' album art instead. Verified against its source (`SsPlaylistParserDelegate`, `Playlist.updateArtworkItems`). A cover set here shows up in Navidrome's own web UI and in this player; it will not show up in Amperfy. Track order, by contrast, it re-reads every time a playlist is opened.

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
