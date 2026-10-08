#!/usr/bin/env python3
"""Нагрузочный прогон живой службы и полная уборка после него (план, раздел 7).

Подкоманды:

    snapshot   записать состояние до прогона (7.4, шаг 1)
    run        сценарии S1–S7 (7.2), JSON-отчёт и короткая таблица
    cleanup    убрать пробы — строго по манифесту .omc/stress/probes.json (7.4, шаг 5)
    residue    сверить с записанным состоянием; код возврата 1 на любой остаток (7.4, шаг 7)

    .venv/bin/python scripts/stress.py snapshot
    .venv/bin/python scripts/stress.py run --scenarios S1,S2,S3,S4,S5
    .venv/bin/python scripts/stress.py run --scenarios S6,S7 --probe-links links.txt --allow-restart
    .venv/bin/python scripts/stress.py cleanup
    .venv/bin/python scripts/stress.py residue

The token comes from adder/.env the way adder/config.py reads it, and never
reaches stdout or a report: the report is checked for it before it is written.
Resources are read from systemd and /proc, not from pgrep -f, which matches its
own command line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent

UNIT = "music-adder"
DEFAULT_BASE_URL = "http://127.0.0.1:8787"
PROBE_PLAYLIST = "zz-stress-probe"
# 429 от YouTube останавливает все скачивания разом (queue.py), поэтому проб мало.
MAX_PROBES = 5
# Те же размеры, что thumbs.SIZES: по ним уборка находит миниатюры проб.
THUMB_SIZES = (96, 300, 600)
ACTIVE_STATUSES = frozenset({"queued", "downloading", "tagging"})
COUNTED_TABLES = ("tasks", "plays", "audio_features", "navidrome_ops")
# Ночные таймеры (03:05/03:30/04:30 + случайная задержка): ни снимок базы, ни
# анализ не должны увидеть пробы.
QUIET_WINDOW = ((2, 45), (5, 15))
PLACEHOLDER_TOKEN = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
SERVER_THREAD = "stress-isolated-server"
ALL_SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
# Пробы сверяются со снимком; старый снимок мог не увидеть то, что добавили после.
SNAPSHOT_MAX_AGE = timedelta(hours=2)
# Имя миниатюры — sha1 в hex (thumb_keys); другое имя из манифеста в путь не идёт.
THUMB_KEY = re.compile(r"[0-9a-f]{40}")
COLD_NOTE = (
    "client and server run in the same process (isolated instance): "
    "compare before/after only, not absolute"
)


@dataclass(frozen=True)
class Paths:
    """Where the live service keeps what the probes can leave behind."""

    repo: Path = REPO

    @property
    def adder(self) -> Path:
        return self.repo / "adder"

    @property
    def env(self) -> Path:
        return self.adder / ".env"

    @property
    def db(self) -> Path:
        return self.adder / "adder.db"

    @property
    def trash(self) -> Path:
        return self.repo / "trash"

    @property
    def thumbs(self) -> Path:
        return self.adder / "thumb-cache"

    @property
    def lyrics(self) -> Path:
        return self.adder / "lyrics-cache"

    @property
    def playlist_history(self) -> Path:
        return self.adder / "playlist-history"

    @property
    def db_snapshots(self) -> Path:
        return self.adder / "snapshots"

    @property
    def state(self) -> Path:
        return self.repo / ".omc" / "stress"

    @property
    def manifest(self) -> Path:
        return self.state / "probes.json"

    @property
    def snapshot(self) -> Path:
        return self.state / "snapshot.json"


# ---------------------------------------------------------------------------
# Pure helpers: numbers, parsing
# ---------------------------------------------------------------------------


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not 0 <= pct <= 100:
        raise ValueError("percentile must be within 0..100")
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


@dataclass
class Sample:
    ms: float
    status: int | None
    size: int
    error: str | None = None


# One request of a load run, called with no arguments.
Job = Callable[[], Awaitable[Sample]]


def summarize(samples: list[Sample], ok: frozenset[int] = frozenset({200, 206})) -> dict:
    times = [s.ms for s in samples]
    statuses: dict[str, int] = {}
    for s in samples:
        key = str(s.status) if s.status is not None else (s.error or "error")
        statuses[key] = statuses.get(key, 0) + 1
    sizes = [s.size for s in samples if s.status in ok]
    return {
        "n": len(samples),
        "errors": sum(1 for s in samples if s.error or s.status not in ok),
        "server_errors": sum(1 for s in samples if s.status is not None and s.status >= 500),
        "statuses": statuses,
        "p50_ms": _round(percentile(times, 50)),
        "p95_ms": _round(percentile(times, 95)),
        "p99_ms": _round(percentile(times, 99)),
        "max_ms": _round(max(times)) if times else None,
        "bytes_mean": _round(sum(sizes) / len(sizes)) if sizes else None,
        "bytes_max": max(sizes) if sizes else None,
    }


def parse_systemctl_show(text: str) -> dict[str, int | None]:
    """``systemctl show -p ...`` output; "[not set]" and "infinity" become None."""
    out: dict[str, int | None] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        try:
            out[key.strip()] = int(value.strip())
        except ValueError:
            out[key.strip()] = None
    return out


def descendants(pid: int, proc: Path = Path("/proc")) -> list[tuple[int, str]]:
    """Every process under ``pid`` as (pid, comm), via /proc/<pid>/task/*/children."""
    found: list[tuple[int, str]] = []
    visited = {pid}
    stack = [pid]
    while stack:
        current = stack.pop()
        for children in sorted((proc / str(current) / "task").glob("*/children")):
            try:
                text = children.read_text()
            except OSError:
                continue  # the thread or the process is gone
            for token in text.split():
                child = int(token)
                if child in visited:
                    continue
                visited.add(child)
                try:
                    comm = (proc / str(child) / "comm").read_text().strip()
                except OSError:
                    comm = "?"
                found.append((child, comm))
                stack.append(child)
    return found


def summarize_resources(samples: list[dict]) -> dict:
    if not samples:
        return {"samples": 0}
    memory = [s["memory"] for s in samples if s.get("memory") is not None]
    cpu = [(s["t"], s["cpu_ns"]) for s in samples if s.get("cpu_ns") is not None]
    cpu_percent = None
    if len(cpu) >= 2 and cpu[-1][0] > cpu[0][0]:
        cpu_percent = (cpu[-1][1] - cpu[0][1]) / 1e9 / (cpu[-1][0] - cpu[0][0]) * 100
    pids = {s.get("pid") for s in samples if s.get("pid")}
    return {
        "samples": len(samples),
        "memory_peak_mb": _round(max(memory) / 2**20) if memory else None,
        "memory_last_mb": _round(memory[-1] / 2**20) if memory else None,
        "cpu_percent": _round(cpu_percent),
        "children_max": max(s.get("children", 0) for s in samples),
        "ffmpeg_max": max(s.get("ffmpeg", 0) for s in samples),
        "main_pids": sorted(p for p in pids if p is not None),
    }


RATE_LIMIT_MARKERS = ("rate_limited", "http error 429", "too many requests", "try again later")
# Строка доступа uvicorn к самому звуку; /api/stream-url сюда не попадает.
STREAM_ACCESS = re.compile(r"GET /api/stream\?")


def rate_limit_lines(journal: str) -> list[str]:
    """Journal lines that say YouTube throttled the service (classify_error's words)."""
    return [
        line for line in journal.splitlines() if any(m in line.lower() for m in RATE_LIMIT_MARKERS)
    ]


def stream_access_lines(journal: str) -> int:
    return sum(1 for line in journal.splitlines() if STREAM_ACCESS.search(line))


def in_quiet_window(now: datetime) -> bool:
    (start_h, start_m), (end_h, end_m) = QUIET_WINDOW
    minutes = now.hour * 60 + now.minute
    return start_h * 60 + start_m <= minutes < end_h * 60 + end_m


_VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/|/v/)([A-Za-z0-9_-]{6,})")


def video_id(url: str) -> str | None:
    found = _VIDEO_ID.search(url or "")
    return found.group(1) if found else None


def parse_probe_links(text: str) -> tuple[list[str], str | None]:
    """The --probe-links file: one YouTube link per line, ``broken: <link>`` once at most.

    The links are the user's choice (short, freely licensed, not in this
    library); the tool itself knows none.
    """
    probes: list[str] = []
    broken: str | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("broken:"):
            if broken is not None:
                raise ValueError(f"line {number}: only one broken link is allowed")
            broken = line.split(":", 1)[1].strip()
            link = broken
        else:
            probes.append(line)
            link = line
        if video_id(link) is None:
            raise ValueError(f"line {number}: not a YouTube video link")
    if not probes:
        raise ValueError("no probe links")
    if len(probes) > MAX_PROBES:
        raise ValueError(f"{len(probes)} probes; at most {MAX_PROBES} (a 429 stops every download)")
    ids = [video_id(link) for link in probes + ([broken] if broken else [])]
    if len(set(ids)) != len(ids):
        raise ValueError("the same video is listed twice")
    return probes, broken


def snapshot_problems(snapshot: dict, library_rows: list[dict], now: datetime) -> list[str]:
    """Why ``snapshot`` cannot be what cleanup deletes against; empty means it can."""
    refresh = "refresh it with `stress.py snapshot --force`"
    problems = []
    try:
        taken = datetime.fromisoformat(snapshot["taken_at"]).astimezone()
    except (KeyError, TypeError, ValueError):
        problems.append(f"the snapshot has no taken_at: {refresh}")
    else:
        if now - taken > SNAPSHOT_MAX_AGE:
            problems.append(f"the snapshot is older than 2 h ({snapshot['taken_at']}): {refresh}")
    before = set(snapshot.get("library_paths") or [])
    current = {r["path"] for r in library_rows}
    if before != current:
        problems.append(
            f"library changed since the snapshot (+{len(current - before)}, "
            f"-{len(before - current)}): {refresh}"
        )
    return problems


def probe_problems(
    links: list[str],
    rate_lines: list[str] | None,
    library_rows: list[dict],
    task_urls: list[str],
    manifest: dict | None,
    snapshot: dict | None,
    playlists: list[str] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Pre-conditions of S6/S7 (7.2, 7.4): anything listed here refuses the run.

    ``playlists`` are the live playlist names, given when S6 will create
    PROBE_PLAYLIST: an existing one of that name is someone's, not a probe's.
    """
    problems = []
    if snapshot is None:
        problems.append("no snapshot: run `stress.py snapshot` first (7.4, step 1)")
    else:
        problems += snapshot_problems(snapshot, library_rows, now or datetime.now().astimezone())
    if playlists is not None:
        names = set(playlists) | set((snapshot or {}).get("playlists") or {})
        if PROBE_PLAYLIST in names:
            problems.append(f"a playlist {PROBE_PLAYLIST!r} already exists; S6 would take it over")
    if manifest_open(manifest):
        problems.append("an earlier probe manifest is not cleaned up: run `stress.py cleanup`")
    if rate_lines is None:
        problems.append("could not read the service journal to rule out a recent rate limit")
    elif rate_lines:
        problems.append(f"YouTube rate limit in the journal within 2 h ({len(rate_lines)} lines)")
    wanted = {video_id(link) for link in links}
    sources = {video_id(r.get("source") or "") for r in library_rows}
    in_library = sorted(v for v in sources & wanted if v)
    if in_library:
        problems.append(f"already in the library: {', '.join(str(v) for v in in_library)}")
    in_tasks = sorted(v for v in {video_id(u) for u in task_urls} & wanted if v)
    if in_tasks:
        problems.append(f"already in the tasks table: {', '.join(str(v) for v in in_tasks)}")
    return problems


def quiet_reasons(
    health: dict | None, stream_lines: int | None, connections: int | None
) -> list[str]:
    """Why the service may not be restarted now (plan 4.0, step 6); empty means go."""
    reasons = []
    if health is None:
        reasons.append("/health did not answer")
    else:
        busy = [t for t in health.get("active_tasks", []) if t.get("status") in ACTIVE_STATUSES]
        if busy:
            reasons.append(f"{len(busy)} active task(s)")
    if stream_lines is None:
        reasons.append("journal unreadable")
    elif stream_lines:
        reasons.append(f"{stream_lines} stream request(s) in the last 60 s")
    if connections is None:
        reasons.append("ss unavailable")
    elif connections:
        reasons.append(f"{connections} established connection(s) to the service")
    return reasons


def make_substrings(titles: list[str], count: int = 200, seed: int = 0) -> list[str]:
    """Search queries of 1-6 letters cut out of real titles, the same every run."""
    rng = random.Random(seed)
    words = [t.lower() for t in titles if t and t.strip()]
    out = []
    for _ in range(count):
        if not words:
            out.append(rng.choice("abcdefghijklmnopqrstuvwxyz"))
            continue
        word = rng.choice(words)
        length = min(rng.randint(1, 6), len(word))
        start = rng.randint(0, len(word) - length)
        out.append(word[start : start + length])
    return out


def thumb_keys(cover: bytes) -> list[str]:
    """Names under thumb-cache that the service gives this cover (thumbs.shrink)."""
    keys = []
    for size in THUMB_SIZES:
        digest = hashlib.sha1(cover)
        digest.update(f"|{size}".encode())
        keys.append(digest.hexdigest())
    return keys


def lyrics_key(rel_path: str) -> str:
    """lyrics._key: the cache file name of one track."""
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Environment and secrets
# ---------------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """adder/.env as python-dotenv parses it; the process environment wins, as in config."""
    values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    for key in ("API_TOKEN", "NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD"):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def api_token(env: dict[str, str]) -> str:
    token = env.get("API_TOKEN", "").strip()
    if not token or token == PLACEHOLDER_TOKEN:
        raise SystemExit("API_TOKEN is not set in adder/.env")
    return token


def secrets_of(env: dict[str, str]) -> list[str]:
    return [v for v in (env.get("API_TOKEN", "").strip(), env.get("NAVIDROME_PASSWORD", "")) if v]


def dump_safely(data: Any, secret_values: Iterable[str]) -> str:
    """JSON text that is refused if any secret ended up in it."""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    for value in secret_values:
        if value and value in text:
            raise RuntimeError("refusing to write a report that contains a secret")
    return text


def write_json(path: Path, data: Any, secret_values: Iterable[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_safely(data, secret_values)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The probe manifest
# ---------------------------------------------------------------------------


def new_manifest(probes: list[str], broken: str | None, restart: str | None = None) -> dict:
    entries = [{"link": link, "kind": "probe"} for link in probes]
    if broken:
        entries.append({"link": broken, "kind": "broken"})
    if restart:
        entries.append({"link": restart, "kind": "restart"})
    return {
        "version": 1,
        "created_at": now_iso(),
        "playlist": PROBE_PLAYLIST,
        "playlist_requested": False,
        "probes": entries,
        "cleaned_at": None,
    }


def manifest_open(manifest: dict | None) -> bool:
    return manifest is not None and manifest.get("cleaned_at") is None


def manifest_task_ids(manifest: dict) -> list[int]:
    return [tid for e in manifest.get("probes", []) for tid in e.get("task_ids", [])]


def manifest_result_paths(manifest: dict) -> list[str]:
    return [e["result_path"] for e in manifest.get("probes", []) if e.get("result_path")]


async def post_probe(client: httpx.AsyncClient, manifest_path: Path, entry: dict) -> list[int]:
    """POST /api/add for one manifest entry -- only once the file on disk lists it."""
    on_disk = read_json(manifest_path) or {}
    if entry["link"] not in [e.get("link") for e in on_disk.get("probes", [])]:
        raise RuntimeError("the manifest must list a probe before /api/add is called")
    entry["posted_at"] = now_iso()
    response = await client.post("/api/add", json={"links": [entry["link"]]})
    response.raise_for_status()
    ids = [int(i) for i in response.json().get("added", [])]
    entry["task_ids"] = ids
    return ids


# ---------------------------------------------------------------------------
# Database (read-only except for the manifest-driven cleanup)
# ---------------------------------------------------------------------------


def db_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    # A finished copy (a nightly snapshot) is opened immutable: in WAL mode even
    # mode=ro leaves -shm/-wal files next to it. The live database is never immutable.
    flags = "immutable=1" if immutable else "mode=ro"
    con = sqlite3.connect(f"{path.resolve().as_uri()}?{flags}", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def table_counts(db_path: Path) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    with contextlib.closing(db_readonly(db_path)) as con:
        for table in COUNTED_TABLES:
            try:
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = None
    return counts


def task_rows(
    db_path: Path, ids: list[int] | None = None, *, immutable: bool = False
) -> list[dict]:
    if not db_path.is_file():
        return []
    with contextlib.closing(db_readonly(db_path, immutable=immutable)) as con:
        if ids is None:
            rows = con.execute("SELECT id, url, status, result_path FROM tasks").fetchall()
        else:
            marks = ",".join("?" * len(ids)) or "NULL"
            rows = con.execute(f"SELECT * FROM tasks WHERE id IN ({marks})", ids).fetchall()
    return [dict(r) for r in rows]


def probe_rows(
    db_path: Path, manifest: dict | None, library_before: set[str], *, immutable: bool = False
) -> dict[str, int]:
    """Rows that belong to the probes: tasks by id or video, plays/features by path."""
    out = {"tasks": 0, "plays": 0, "audio_features": 0}
    if manifest is None or not db_path.is_file():
        return out
    ids = set(manifest_task_ids(manifest))
    videos = {video_id(e["link"]) for e in manifest.get("probes", [])} - {None}
    probe_paths = [p for p in manifest_result_paths(manifest) if p not in library_before]
    out["tasks"] = sum(
        1
        for r in task_rows(db_path, immutable=immutable)
        if r["id"] in ids or video_id(r["url"] or "") in videos
    )
    with contextlib.closing(db_readonly(db_path, immutable=immutable)) as con:
        for table in ("plays", "audio_features"):
            for path in probe_paths:
                out[table] += con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE path = ?", (path,)
                ).fetchone()[0]
    return out


# ---------------------------------------------------------------------------
# Navidrome (its native API; never sent the service token)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Navidrome:
    url: str
    user: str
    password: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Navidrome | None:
        user, password = env.get("NAVIDROME_USER", "").strip(), env.get("NAVIDROME_PASSWORD", "")
        if not user or not password:
            return None
        url = env.get("NAVIDROME_URL", "http://127.0.0.1:4533").rstrip("/")
        return cls(url, user, password) if url else None


async def navidrome_state(client: httpx.AsyncClient, nd: Navidrome | None) -> dict:
    """Missing-file count (admin "Missing Files", /api/missing) and playlist names.

    Each is None when it cannot be read; residue then stays red until a person
    has looked.
    """
    state: dict[str, Any] = {"missing": None, "playlists": None, "note": None}
    if nd is None:
        state["note"] = "Navidrome credentials are not in adder/.env"
        return state
    try:
        login = await client.post(
            f"{nd.url}/auth/login", json={"username": nd.user, "password": nd.password}
        )
        login.raise_for_status()
        headers = {"x-nd-authorization": f"Bearer {login.json()['token']}"}
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        state["note"] = f"Navidrome login failed ({type(exc).__name__})"
        return state
    try:
        missing = await client.get(
            f"{nd.url}/api/missing", params={"_start": 0, "_end": 1}, headers=headers
        )
        missing.raise_for_status()
        state["missing"] = int(missing.headers["x-total-count"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        state["note"] = f"missing-file count unavailable ({type(exc).__name__})"
    try:
        listing = await client.get(f"{nd.url}/api/playlist", headers=headers)
        listing.raise_for_status()
        state["playlists"] = sorted(str(p.get("name")) for p in listing.json() or [])
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        state["note"] = f"playlists unavailable ({type(exc).__name__})"
    return state


# ---------------------------------------------------------------------------
# Snapshot and residue
# ---------------------------------------------------------------------------


def probe_missing_ids(missing: list[dict], probe_paths: set[str]) -> list[str]:
    """Ids of Navidrome's "missing" entries that are exactly the probes' files."""
    return [str(m["id"]) for m in missing if m.get("id") and m.get("path") in probe_paths]


async def navidrome_auth(client: httpx.AsyncClient, nd: Navidrome) -> dict[str, str]:
    login = await client.post(
        f"{nd.url}/auth/login", json={"username": nd.user, "password": nd.password}
    )
    login.raise_for_status()
    return {"x-nd-authorization": f"Bearer {login.json()['token']}"}


async def navidrome_never_scanned(
    client: httpx.AsyncClient, nd: Navidrome, probe_paths: set[str]
) -> set[str]:
    """Probe paths Navidrome has no song for at all, live or missing.

    A probe deleted before Navidrome's scanner reached it never shows up in
    /api/missing. ``GET /api/song?path=`` (its react-admin filter, a
    ``LIKE 'path%'`` over media_file, missing rows included) tells the two
    apart; only an exact path counts. Raises when Navidrome cannot be asked.
    """
    headers = await navidrome_auth(client, nd)
    unknown = set()
    for path in sorted(probe_paths):
        response = await client.get(
            f"{nd.url}/api/song",
            params={"path": path, "_start": 0, "_end": 100},
            headers=headers,
        )
        response.raise_for_status()
        if not any(row.get("path") == path for row in response.json() or []):
            unknown.add(path)
    return unknown


async def purge_navidrome_probes(
    client: httpx.AsyncClient,
    nd: Navidrome | None,
    probe_paths: set[str],
    wait: float = 60.0,
    step: float = 5.0,
) -> list[str]:
    """7.4 step 6: Navidrome keeps deleted files as "missing"; remove the probes'.

    Only entries whose path is one of the probes' result paths are touched
    (``DELETE /api/missing?id=…``, what its admin "Missing Files" page sends).
    Its scanner notices a deletion some seconds later, so this polls for
    ``wait`` seconds. Returns the paths it removed.
    """
    if nd is None or not probe_paths:
        return []
    headers = await navidrome_auth(client, nd)
    removed: list[str] = []
    deadline = time.monotonic() + wait
    while True:
        listing = await client.get(
            f"{nd.url}/api/missing", params={"_start": 0, "_end": 5000}, headers=headers
        )
        listing.raise_for_status()
        rows = listing.json() or []
        ids = probe_missing_ids(rows, probe_paths)
        if ids:
            response = await client.delete(
                f"{nd.url}/api/missing", params=[("id", i) for i in ids], headers=headers
            )
            response.raise_for_status()
            removed += [m["path"] for m in rows if str(m.get("id")) in ids]
        if set(removed) >= probe_paths or time.monotonic() >= deadline:
            return removed
        await asyncio.sleep(step)


async def collect_state(
    client: httpx.AsyncClient,
    nd_client: httpx.AsyncClient,
    nd: Navidrome | None,
    paths: Paths,
    manifest: dict | None = None,
    library_before: set[str] | None = None,
) -> dict:
    library_rows = await get_library(client)
    listing = await client.get("/api/playlists")
    listing.raise_for_status()
    navidrome = await navidrome_state(nd_client, nd)
    state = {
        "taken_at": now_iso(),
        "library_paths": sorted(r["path"] for r in library_rows),
        "playlists": {p["name"]: p.get("revision") for p in listing.json()},
        "counts": table_counts(paths.db),
        "max_task_id": max((r["id"] for r in task_rows(paths.db)), default=0),
        "navidrome_missing": navidrome["missing"],
        "navidrome_playlists": navidrome["playlists"],
        "navidrome_note": navidrome["note"],
    }
    if manifest is not None:
        before = library_before or set()
        state.update(await service_queue_state(client))
        state["probe_rows"] = probe_rows(paths.db, manifest, before)
        state["leftover_files"] = leftover_files(paths, manifest, before)
        state["db_snapshot"] = db_snapshot_check(paths, manifest, before)
    return state


async def service_queue_state(client: httpx.AsyncClient) -> dict:
    """7.4.6, the cheap part: /health's navidrome_pending and /api/sync rows of the probe.

    GET /api/sync only reads what the last pass found; POST would run one.
    Each value is None when it cannot be read.
    """
    out: dict[str, Any] = {"navidrome_pending": None, "sync_probe": None}
    try:
        response = await client.get("/health")
        response.raise_for_status()
        pending = response.json().get("navidrome_pending")
        out["navidrome_pending"] = int(pending) if pending is not None else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    try:
        response = await client.get("/api/sync")
        response.raise_for_status()
        rows = (response.json().get("last_result") or {}).get("playlists") or []
        out["sync_probe"] = [r for r in rows if r.get("playlist") == PROBE_PLAYLIST]
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return out


def valid_thumb_keys(entry: dict) -> tuple[list[str], list[str]]:
    """The entry's thumb keys split into (sha1 names, anything else)."""
    keys = [str(k) for k in entry.get("thumb_keys") or []]
    good = [k for k in keys if THUMB_KEY.fullmatch(k)]
    return good, [k for k in keys if not THUMB_KEY.fullmatch(k)]


def leftover_files(paths: Paths, manifest: dict, library_before: set[str]) -> list[str]:
    left = []
    history = paths.playlist_history / PROBE_PLAYLIST
    if manifest.get("playlist_requested") and history.exists():
        left.append(str(history))
    for entry in manifest.get("probes", []):
        trash = entry.get("trash")
        if trash and Path(trash).exists():
            left.append(trash)
        result = entry.get("result_path")
        if not result or result in library_before:
            continue
        for key in valid_thumb_keys(entry)[0]:
            if (paths.thumbs / f"{key}.jpg").exists():
                left.append(str(paths.thumbs / f"{key}.jpg"))
        lyric = paths.lyrics / f"{lyrics_key(result)}.json"
        if lyric.exists():
            left.append(str(lyric))
    return left


def db_snapshot_check(paths: Paths, manifest: dict, library_before: set[str]) -> dict:
    """The nightly adder/snapshots/adder_*.db taken after the cleanup must hold no probe."""
    cleaned = manifest.get("cleaned_at")
    found = sorted(paths.db_snapshots.glob("adder_*.db"), key=lambda p: p.stat().st_mtime)
    if not cleaned or not found:
        return {"checked": False, "reason": "no cleanup yet or no snapshots"}
    newest = found[-1]
    if newest.stat().st_mtime <= datetime.fromisoformat(cleaned).timestamp():
        return {"checked": False, "reason": f"{newest.name} predates the cleanup; check tomorrow"}
    return {
        "checked": True,
        "file": newest.name,
        "rows": probe_rows(newest, manifest, library_before, immutable=True),
    }


def residue_problems(snapshot: dict, current: dict) -> list[str]:
    """Everything that differs from the pre-test state (7.4, step 7). Empty means clean."""
    problems = []
    before, now = set(snapshot["library_paths"]), set(current["library_paths"])
    if added := sorted(now - before):
        problems.append(f"library: {len(added)} track(s) not in the snapshot: {added[:10]}")
    if removed := sorted(before - now):
        problems.append(f"library: {len(removed)} track(s) gone since the snapshot: {removed[:10]}")
    for name in sorted(set(snapshot["playlists"]) | set(current["playlists"])):
        was, is_now = snapshot["playlists"].get(name), current["playlists"].get(name)
        if was != is_now:
            problems.append(f"playlist {name!r}: revision {was} -> {is_now}")
    if current["navidrome_playlists"] is None:
        problems.append(
            f"Navidrome playlists unknown ({current.get('navidrome_note')}): "
            f"check by hand that {PROBE_PLAYLIST!r} is gone"
        )
    elif PROBE_PLAYLIST in current["navidrome_playlists"]:
        problems.append(f"Navidrome still has playlist {PROBE_PLAYLIST!r}")
    for table in COUNTED_TABLES:
        was, is_now = snapshot["counts"].get(table), current["counts"].get(table)
        if was != is_now:
            problems.append(f"table {table}: {was} rows in the snapshot, {is_now} now")
    for table, count in (current.get("probe_rows") or {}).items():
        if count:
            problems.append(f"table {table}: {count} row(s) of the probes")
    was, is_now = snapshot.get("navidrome_missing"), current.get("navidrome_missing")
    if was is None or is_now is None:
        problems.append(
            "Navidrome missing-file count unknown "
            f"(snapshot {was}, now {is_now}; {current.get('navidrome_note')}): "
            "open Navidrome -> Missing Files and check by hand that no probe is listed"
        )
    elif is_now != was:
        problems.append(
            f"Navidrome missing files: {was} before, {is_now} now -- "
            "purge the probes there (7.4, step 6)"
        )
    for path in current.get("leftover_files") or []:
        problems.append(f"file left behind: {path}")
    if "navidrome_pending" in current:
        pending = current["navidrome_pending"]
        if pending is None:
            problems.append("navidrome_pending unknown: /health did not say")
        elif pending:
            problems.append(f"/health navidrome_pending is {pending}: Navidrome work still queued")
    if "sync_probe" in current:
        if current["sync_probe"] is None:
            problems.append("GET /api/sync unreadable: check the sync state by hand")
        for row in current["sync_probe"] or []:
            problems.append(f"/api/sync: {PROBE_PLAYLIST!r} diverged ({row.get('reason')})")
    check = current.get("db_snapshot") or {}
    for table, count in (check.get("rows") or {}).items():
        if count:
            problems.append(
                f"nightly snapshot {check.get('file')}: {count} probe row(s) in {table}"
            )
    return problems


# ---------------------------------------------------------------------------
# Cleanup, driven only by the manifest
# ---------------------------------------------------------------------------


def remove_trash_file(file: Path, trash_root: Path) -> bool:
    """Delete one file the service moved to trash/, then its now-empty folders."""
    root = trash_root.resolve()
    target = file.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"not inside {root}: {file}")
    if not target.exists():
        return False
    target.unlink()
    for parent in target.parents:
        if parent == root:
            break
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        else:
            break
    return True


async def refresh_tasks(client: httpx.AsyncClient, manifest: dict, db_path: Path) -> None:
    """Fill status, error_type and result_path of each probe from /api/tasks (or the DB)."""
    ids = manifest_task_ids(manifest)
    if not ids:
        return
    response = await client.get("/api/tasks")
    response.raise_for_status()
    rows = {r["id"]: r for r in response.json() if r.get("id") in ids}
    missing = [i for i in ids if i not in rows]
    if missing:
        rows.update({r["id"]: r for r in task_rows(db_path, missing)})
    apply_task_rows(manifest, rows)


async def record_thumb_keys(client: httpx.AsyncClient, entry: dict) -> None:
    if entry.get("thumb_keys") or not entry.get("result_path"):
        return
    response = await client.get("/api/cover", params={"path": entry["result_path"]})
    if response.status_code == 200:
        entry["thumb_keys"] = thumb_keys(response.content)


def ownership_problem(entry: dict, task_rows_: list[dict], source: str | None) -> str | None:
    """Why the entry's result_path may not be deleted as the probe's own track.

    Its tasks row (one of the entry's task ids, pointing at this path) must be
    for the probe's video, and so must the file's own source tag. None: it may.
    """
    result, want = entry["result_path"], video_id(entry["link"])
    rows = [r for r in task_rows_ if r.get("result_path") == result]
    if not any(video_id(r.get("url") or "") == want for r in rows):
        return f"{result!r}: no tasks row of this probe's video points at it; left alone"
    if video_id(source or "") != want:
        return f"{result!r}: its source tag is not this probe's video; left alone"
    return None


async def cleanup(
    client: httpx.AsyncClient, paths: Paths, manifest: dict, snapshot: dict | None
) -> list[str]:
    """7.4 step 5. Touches only what the manifest names; returns what went wrong.

    It never marks the manifest clean: finish_cleanup does, after Navidrome.
    """
    errors: list[str] = []
    notes: list[str] = []

    def save() -> None:
        write_json(paths.manifest, manifest)

    if snapshot is None:
        errors.append("no snapshot: library tracks are deleted only against one")
    library_before = set(snapshot["library_paths"]) if snapshot else None

    await refresh_tasks(client, manifest, paths.db)
    save()

    # Удаляется только подборка с постоянным именем: имя из манифеста в путь
    # и в DELETE не идёт.
    named = manifest.get("playlist", PROBE_PLAYLIST)
    if named != PROBE_PLAYLIST:
        errors.append(f"the manifest names playlist {named!r}, not {PROBE_PLAYLIST!r}; left alone")
    elif manifest.get("playlist_requested") and not manifest.get("playlist_deleted"):
        response = await client.delete(f"/api/playlists/{quote(PROBE_PLAYLIST, safe='')}")
        if response.status_code in (200, 404):
            manifest["playlist_deleted"] = True
        else:
            errors.append(f"DELETE playlist {PROBE_PLAYLIST!r}: HTTP {response.status_code}")
        save()

    if named == PROBE_PLAYLIST and manifest.get("playlist_deleted"):
        # Служба хранит прежние версии каждой подборки; у пробной их быть не должно.
        history = paths.playlist_history / PROBE_PLAYLIST
        if history.is_dir() and history.resolve().parent == paths.playlist_history.resolve():
            shutil.rmtree(history)

    sources: dict[str, str] | None = None

    for entry in manifest.get("probes", []):
        result = entry.get("result_path")
        if not result or entry.get("deleted"):
            continue
        if entry.get("status") in ACTIVE_STATUSES:
            errors.append(
                f"task {entry.get('task_ids')} is still {entry['status']}: run again later"
            )
            continue
        if library_before is None:
            continue
        if result in library_before:
            # Дубликат: задача «done» указывает на трек, который был до теста.
            entry["kept_existing"] = True
            notes.append(f"{result}: was in the library before the test, left alone")
            save()
            continue
        if sources is None:
            try:
                sources = {r["path"]: r.get("source") or "" for r in await get_library(client)}
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                errors.append(
                    f"library listing unavailable ({type(exc).__name__}); nothing deleted"
                )
                break
        if result not in sources:
            # Трека в фонотеке уже нет (как 404 у DELETE).
            entry["deleted"] = True
            save()
            continue
        problem = ownership_problem(
            entry, task_rows(paths.db, entry.get("task_ids", [])), sources[result]
        )
        if problem:
            errors.append(problem)
            continue
        await record_thumb_keys(client, entry)
        response = await client.request("DELETE", "/api/library", json={"path": result})
        if response.status_code == 200:
            entry["trash"] = response.json().get("trash")
            entry["deleted"] = True
        elif response.status_code == 404:
            entry["deleted"] = True
        else:
            errors.append(f"DELETE /api/library {result!r}: HTTP {response.status_code}")
        save()

    for entry in manifest.get("probes", []):
        if entry.get("trash") and not entry.get("trash_removed"):
            try:
                remove_trash_file(Path(entry["trash"]), paths.trash)
                entry["trash_removed"] = True
            except (OSError, ValueError) as exc:
                errors.append(f"trash {entry['trash']}: {exc}")
            save()

    errors += remove_probe_leftovers(paths, manifest, library_before)

    manifest["cleanup_notes"] = notes
    save()
    return errors


def remove_probe_leftovers(
    paths: Paths, manifest: dict, library_before: set[str] | None
) -> list[str]:
    """Rows, thumbnails and lyrics of the probes. Safe to repeat: the service
    keeps working on a new track in the background (lyrics, analysis) and can
    write after the probe is gone."""
    errors = delete_probe_rows(paths.db, manifest, library_before)
    for entry in manifest.get("probes", []):
        result = entry.get("result_path")
        if not result or library_before is None or result in library_before:
            continue
        keys, bad = valid_thumb_keys(entry)
        if bad:
            errors.append(f"thumb key(s) {bad!r} in the manifest are not sha1 names; left alone")
        targets = [paths.thumbs / f"{k}.jpg" for k in keys]
        targets.append(paths.lyrics / f"{lyrics_key(result)}.json")
        for target in targets:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
    return errors


def delete_probe_rows(db_path: Path, manifest: dict, library_before: set[str] | None) -> list[str]:
    """tasks rows of the probes (by id, and only if the row is that probe's video),
    plus plays/audio_features rows of probe tracks that were not there before.

    Without a snapshot (``library_before`` None) no path-keyed row is touched:
    a probe that turned out to be a duplicate points at a real track.
    """
    if not db_path.is_file():
        return [f"{db_path} not found"]
    errors = []
    con = sqlite3.connect(db_path, timeout=30)
    try:
        with con:
            for entry in manifest.get("probes", []):
                if entry.get("result_path") and not (
                    entry.get("deleted") or entry.get("kept_existing")
                ):
                    # Пока трек на месте, строка задачи — то, чем он проверяется.
                    continue
                for tid in entry.get("task_ids", []):
                    row = con.execute(
                        "SELECT url, status FROM tasks WHERE id = ?", (tid,)
                    ).fetchone()
                    if row is None:
                        continue
                    if video_id(row[0] or "") != video_id(entry["link"]):
                        errors.append(f"tasks row {tid} is not this probe's link; left alone")
                        continue
                    if row[1] in ACTIVE_STATUSES:
                        errors.append(f"tasks row {tid} is still {row[1]}; left alone")
                        continue
                    con.execute("DELETE FROM tasks WHERE id = ?", (tid,))
            for path in manifest_result_paths(manifest):
                if library_before is None or path in library_before:
                    continue
                for table in ("plays", "audio_features"):
                    with contextlib.suppress(sqlite3.OperationalError):
                        con.execute(f"DELETE FROM {table} WHERE path = ?", (path,))
    finally:
        con.close()
    return errors


# ---------------------------------------------------------------------------
# Load generation
# ---------------------------------------------------------------------------


def make_client(base_url: str, token: str, timeout: float = 60.0) -> httpx.AsyncClient:
    # trust_env=False: служба на этой машине, прокси из окружения ей ни к чему.
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
        trust_env=False,
    )


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


async def timed_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> Sample:
    start = time.perf_counter()
    try:
        response = await client.get(url, params=params)
        body = response.content
    except httpx.HTTPError as exc:
        return Sample(_elapsed_ms(start), None, 0, type(exc).__name__)
    return Sample(_elapsed_ms(start), response.status_code, len(body))


async def first_byte(client: httpx.AsyncClient, url: str, byte_range: str) -> Sample:
    """Time to the first audio bytes of a Range request, as a seeking player sees it."""
    start = time.perf_counter()
    try:
        async with client.stream("GET", url, headers={"Range": byte_range}) as response:
            size = 0
            async for chunk in response.aiter_raw():
                size = len(chunk)
                break
            return Sample(_elapsed_ms(start), response.status_code, size)
    except httpx.HTTPError as exc:
        return Sample(_elapsed_ms(start), None, 0, type(exc).__name__)


async def run_pool(jobs: Sequence[Job], concurrency: int) -> list[Sample]:
    pending = iter(jobs)
    out: list[Sample] = []

    async def worker() -> None:
        for job in pending:
            out.append(await job())

    await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, len(jobs))))))
    return out


def service_probe(unit: str = UNIT) -> dict | None:
    try:
        done = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "MemoryCurrent", "-p", "CPUUsageNSec", "-p", "MainPID"],
            capture_output=True, text=True, timeout=5,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    values = parse_systemctl_show(done.stdout)
    pid = values.get("MainPID") or 0
    kids = descendants(pid) if pid else []
    return {
        "memory": values.get("MemoryCurrent"),
        "cpu_ns": values.get("CPUUsageNSec"),
        "pid": pid,
        "children": len(kids),
        "ffmpeg": sum(1 for _, comm in kids if comm == "ffmpeg"),
    }


def own_probe() -> dict:
    """This process (the isolated instance lives in it): its ffmpeg children."""
    kids = descendants(os.getpid())
    return {"ffmpeg": sum(1 for _, comm in kids if comm == "ffmpeg"), "children": 0}


class Sampler:
    """Samples a probe function in a thread until the block ends."""

    def __init__(self, probe: Callable[[], dict | None], interval: float = 0.5):
        self.probe = probe
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _take(self) -> None:
        sample = await asyncio.to_thread(self.probe)
        if sample is not None:
            sample["t"] = time.monotonic()
            self.samples.append(sample)

    async def _loop(self) -> None:
        while True:
            await self._take()
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval)
            except TimeoutError:
                continue
            await self._take()
            return

    async def __aenter__(self) -> Sampler:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    def summary(self) -> dict:
        return summarize_resources(self.samples)


async def measured(
    label: str,
    jobs: Sequence[Job],
    concurrency: int,
    probe: Callable[[], dict | None] = service_probe,
    ok: frozenset[int] = frozenset({200, 206}),
) -> dict:
    async with Sampler(probe) as sampler:
        start = time.perf_counter()
        samples = await run_pool(jobs, concurrency)
        wall = time.perf_counter() - start
    return {
        "label": label,
        "concurrency": concurrency,
        "wall_s": _round(wall),
        **summarize(samples, ok),
        "resources": sampler.summary(),
    }


async def get_library(client: httpx.AsyncClient) -> list[dict]:
    response = await client.get("/api/library", params={"limit": 1_000_000, "sort": "name"})
    response.raise_for_status()
    return list(response.json())


def sample_paths(rows: list[dict], count: int, seed: int = 0) -> list[str]:
    paths = [r["path"] for r in rows]
    return random.Random(seed).sample(paths, min(count, len(paths)))


async def signed_urls(client: httpx.AsyncClient, track_paths: list[str]) -> list[str]:
    urls = []
    for path in track_paths:
        response = await client.get("/api/stream-url", params={"path": path})
        if response.status_code == 200:
            urls.append(response.json()["url"])
    return urls


def range_offset(rng: random.Random, size: int | None, chunk: int = 65536) -> int:
    """Начало Range-запроса, при котором весь кусок лежит внутри файла."""
    if not size or size <= chunk:
        return 0
    return rng.randrange(0, size - chunk)


async def file_size(client: httpx.AsyncClient, url: str) -> int | None:
    """Размер файла из Content-Range ответа на bytes=0-0 (None — не сказал)."""
    try:
        response = await client.get(url, headers={"Range": "bytes=0-0"})
    except httpx.HTTPError:
        return None
    total = response.headers.get("content-range", "").rpartition("/")[2]
    return int(total) if total.isdigit() else None


async def stream_loop(
    client: httpx.AsyncClient, urls: list[str], stop: asyncio.Event, rounds: int | None = None
) -> list[Sample]:
    """Rounds of len(urls) parallel Range requests until ``stop`` is set (or ``rounds`` ran)."""
    rng = random.Random(1)
    samples: list[Sample] = []
    done = 0
    # Смещение — внутри файла: 50-секундный трек весит полтора мегабайта, и
    # случайное «до 2 МБ» давало честный 416 сервера, а не ошибку службы.
    sizes = await asyncio.gather(*(file_size(client, u) for u in urls)) if urls else []
    while urls and not stop.is_set() and (rounds is None or done < rounds):
        offsets = [range_offset(rng, size) for size in sizes]
        samples += await asyncio.gather(
            *(
                first_byte(client, u, f"bytes={o}-{o + 65535}")
                for u, o in zip(urls, offsets, strict=True)
            )
        )
        done += 1
    return samples


# ---------------------------------------------------------------------------
# Scenarios S1-S5 (read-only against the live service)
# ---------------------------------------------------------------------------


async def scenario_s1(client: httpx.AsyncClient, requests: int = 200) -> dict:
    runs = []
    for limit in (100000, 200):
        for concurrency in (1, 5, 20):
            params = {"limit": limit, "sort": "new"}
            jobs = [partial(timed_get, client, "/api/library", params) for _ in range(requests)]
            runs.append(await measured(f"library limit={limit}", jobs, concurrency))
    return {"runs": runs}


async def scenario_s2(client: httpx.AsyncClient, rows: list[dict]) -> dict:
    queries = make_substrings([r.get("title") or "" for r in rows])
    jobs = [
        partial(timed_get, client, "/api/library", {"q": q, "limit": 200, "sort": "new"})
        for q in queries
    ]
    return {"runs": [await measured("library search", jobs, 5)]}


def _cover_jobs(client: httpx.AsyncClient, paths: list[str], size: int) -> list[Job]:
    return [partial(timed_get, client, "/api/cover", {"path": p, "size": size}) for p in paths]


COVER_OK = frozenset({200, 404})  # 404: у трека нет обложки, это не сбой


async def scenario_s3_warm(client: httpx.AsyncClient, paths: list[str]) -> list[dict]:
    runs = []
    for size in (96, 300):
        # Первый проход прогревает кэш миниатюр живой службы; он тоже в отчёте.
        jobs = _cover_jobs(client, paths, size)
        runs.append(await measured(f"covers warm-up size={size}", jobs, 6, ok=COVER_OK))
        for concurrency in (6, 24):
            jobs = _cover_jobs(client, paths, size)
            runs.append(await measured(f"covers warm size={size}", jobs, concurrency, ok=COVER_OK))
    return runs


async def scenario_s3_cold(paths: list[str], root: Path) -> list[dict]:
    runs = []
    with IsolatedInstance(root) as instance:
        async with make_client(instance.url, instance.token) as client:
            for size in (96, 300):
                for concurrency in (6, 24):
                    instance.clear_thumbs()
                    jobs = _cover_jobs(client, paths, size)
                    label = f"covers cold (isolated) size={size}"
                    run = await measured(label, jobs, concurrency, own_probe, COVER_OK)
                    runs.append({**run, "note": COLD_NOTE})
    return runs


async def scenario_s5(client: httpx.AsyncClient, seconds: float) -> dict:
    endpoints = ["/api/home", "/api/playlists", "/health", "/api/tasks"]
    deadline = time.monotonic() + seconds
    by_endpoint: dict[str, list[Sample]] = {e: [] for e in endpoints}

    async def worker(offset: int) -> None:
        i = offset
        while time.monotonic() < deadline:
            endpoint = endpoints[i % len(endpoints)]
            by_endpoint[endpoint].append(await timed_get(client, endpoint))
            i += 1

    async with Sampler(service_probe) as sampler:
        await asyncio.gather(*(worker(i) for i in range(10)))
    every = [s for samples in by_endpoint.values() for s in samples]
    return {
        "runs": [
            {"label": f"mixed {e}", "concurrency": 10, **summarize(by_endpoint[e])}
            for e in endpoints
        ]
        + [{"label": "mixed all", "concurrency": 10, **summarize(every)}],
        "resources": sampler.summary(),
        "server_errors": sum(1 for s in every if s.status is not None and s.status >= 500),
    }


# ---------------------------------------------------------------------------
# The isolated in-process instance for cold covers (7.2)
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


RUNTIME_PATHS = {
    "TMP_DIR": "tmp",
    "DB_PATH": "adder.db",
    "TRASH_DIR": "trash",
    "OUTSIDE_DIR": "outside-cache",
    "THUMB_DIR": "thumb-cache",
    "WEB_COVERS_DIR": "web-covers",
    "ARTIST_PHOTOS_DIR": "artist-photos",
    "ARTIST_IMPORT_FILE": "artist-import.json",
    "CACHE_DIR": "cache",
}
# Пути, которые модули сняли с runtime при импорте: переназначение runtime их не задевает.
MODULE_COPIES = {
    ("lyrics", "CACHE_DIR"): "lyrics-cache",
    ("covers", "COVERS_DIR"): "playlist-covers",
    ("playlists", "HISTORY_DIR"): "playlist-history",
}


class IsolatedInstance:
    """The app in this process, on a free port, with lifespan off and every
    writable path in ``root``.

    lifespan would start the workers, the artist import (live adder/
    artist-import.json, downloads into the live library), the lyrics walk,
    ListenBrainz and the auto-requeue; with it off none of them run. Only
    /api/cover is meant to be asked of it. The library is the configured one
    (read only) unless ``library`` is given.
    """

    join_timeout = 15.0

    def __init__(self, root: Path, library: Path | None = None):
        self.root = root
        self.library = library
        self.url = ""
        self.token = ""
        self._saved: list[tuple[Any, str, Any]] = []
        self._env: dict[str, str | None] = {}
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def _set(self, owner: Any, name: str, value: Any) -> None:
        self._saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def __enter__(self) -> IsolatedInstance:
        if "adder.config" not in sys.modules:
            # До импорта: config читает окружение один раз. MAX_WORKERS не
            # трогается — ноль config запрещает, а воркеры запускает lifespan.
            fresh = {
                "API_TOKEN": secrets.token_urlsafe(32),
                "NAVIDROME_URL": "",
                "NAVIDROME_USER": "",
                "NAVIDROME_PASSWORD": "",
                "LISTENBRAINZ_TOKEN": "",
            }
            for key, value in fresh.items():
                self._env[key] = os.environ.get(key)
                os.environ[key] = value
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        import importlib

        import uvicorn

        from adder import app as app_module
        from adder import config, runtime

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for name, relative in RUNTIME_PATHS.items():
                self._set(runtime, name, self.root / relative)
            for (module, name), relative in MODULE_COPIES.items():
                self._set(importlib.import_module(f"adder.{module}"), name, self.root / relative)
            for name in ("NAVIDROME_USER", "NAVIDROME_PASSWORD", "LISTENBRAINZ_TOKEN"):
                self._set(config, name, "")
            if self.library is not None:
                self._set(config, "LIBRARY", self.library)
            self.token = config.API_TOKEN
            port = free_port()
            self._server = uvicorn.Server(
                uvicorn.Config(
                    app_module.app,
                    host="127.0.0.1",
                    port=port,
                    lifespan="off",
                    log_level="warning",
                    access_log=False,
                )
            )
            self._thread = threading.Thread(
                target=self._server.run, name=SERVER_THREAD, daemon=True
            )
            self._thread.start()
            deadline = time.monotonic() + 20
            while not self._server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            if not self._server.started:
                raise RuntimeError("the isolated instance did not start")
            self.url = f"http://127.0.0.1:{port}"
        except BaseException:
            self.__exit__()
            raise
        return self

    def clear_thumbs(self) -> None:
        from adder import runtime

        root, target = self.root.resolve(), Path(runtime.THUMB_DIR).resolve()
        if target == root or not target.is_relative_to(root):
            raise RuntimeError(f"thumb-cache {target} is not inside the instance's {root}")
        shutil.rmtree(target, ignore_errors=True)

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=self.join_timeout)
            if self._thread.is_alive():
                # Вернуть живые пути под работающим сервером — значит дать ему
                # писать в настоящие adder/ и trash/. Пути остаются временными.
                message = "the isolated server is still running; live paths NOT restored"
                print(f"stress: {message}", file=sys.stderr)
                raise RuntimeError(message)
        for owner, name, value in reversed(self._saved):
            setattr(owner, name, value)
        self._saved.clear()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._env.clear()


# ---------------------------------------------------------------------------
# S6 probes and S7 restart
# ---------------------------------------------------------------------------


async def wait_for_tasks(
    client: httpx.AsyncClient, ids: list[int], poll: float, timeout: float
) -> dict[int, dict]:
    """Poll /api/tasks until every id is done or failed; returns the last rows seen."""
    deadline = time.monotonic() + timeout
    seen: dict[int, dict] = {}
    finished_at: dict[int, float] = {}
    start = time.monotonic()
    while ids:
        response = await client.get("/api/tasks")
        if response.status_code == 200:
            for row in response.json():
                if row.get("id") in ids:
                    seen[row["id"]] = row
                    if row.get("status") not in ACTIVE_STATUSES and row["id"] not in finished_at:
                        finished_at[row["id"]] = time.monotonic() - start
        if all(i in finished_at for i in ids) or time.monotonic() > deadline:
            break
        await asyncio.sleep(poll)
    for tid, seconds in finished_at.items():
        seen[tid]["seconds"] = _round(seconds)
    return seen


def apply_task_rows(manifest: dict, rows: dict[int, dict]) -> None:
    for entry in manifest.get("probes", []):
        for tid in entry.get("task_ids", []):
            row = rows.get(tid)
            if row is None:
                continue
            entry["status"] = row.get("status")
            entry["error_type"] = row.get("error_type") or None
            if row.get("seconds") is not None:
                entry["seconds"] = row["seconds"]
            if row.get("result_path"):
                entry["result_path"] = row["result_path"]


async def scenario_s6(
    client: httpx.AsyncClient,
    paths: Paths,
    manifest: dict,
    stream_urls: list[str],
    poll: float = 3.0,
    timeout: float = 900.0,
) -> dict:
    """Probes (7.4) while S4's streams run. The manifest is on disk before any /api/add."""

    def save() -> None:
        write_json(paths.manifest, manifest)

    save()
    entries = [e for e in manifest["probes"] if e["kind"] in ("probe", "broken")]
    stop = asyncio.Event()
    streams = asyncio.create_task(stream_loop(client, stream_urls, stop))
    async with Sampler(service_probe) as sampler:
        try:
            ids: list[int] = []
            for entry in entries:
                ids += await post_probe(client, paths.manifest, entry)
                save()
            first = next(e for e in entries if e["kind"] == "probe")
            repeat = {"link": first["link"], "kind": "repeat"}
            manifest["probes"].append(repeat)
            save()
            ids += await post_probe(client, paths.manifest, repeat)
            save()

            created = await client.post(
                "/api/playlists", json={"name": PROBE_PLAYLIST, "paths": []}
            )
            if created.is_success:
                # Только своя: 409 значит, что подборка с этим именем уже чья-то.
                manifest["playlist_requested"] = True
                save()
            created.raise_for_status()

            rows = await wait_for_tasks(client, ids, poll, timeout)
            apply_task_rows(manifest, rows)
            save()
            for entry in manifest["probes"]:
                await record_thumb_keys(client, entry)
            save()
            done = [e["result_path"] for e in entries if e.get("status") == "done"]
            put = await client.put(
                f"/api/playlists/{quote(PROBE_PLAYLIST, safe='')}/tracks",
                json={"paths": done, "revision": created.json().get("revision")},
            )
        finally:
            stop.set()
            stream_samples = await streams
    broken = [e for e in entries if e["kind"] == "broken"]
    probes = [e for e in entries if e["kind"] == "probe"]
    return {
        "probes": [
            {k: e.get(k) for k in ("kind", "task_ids", "status", "error_type", "seconds")}
            for e in manifest["probes"]
        ],
        "checks": {
            "probes_done": all(e.get("status") == "done" for e in probes),
            "broken_is_youtube_not_found": all(
                e.get("error_type") == "youtube_not_found" for e in broken
            )
            if broken
            else None,
            "repeat_not_queued": repeat.get("task_ids") == [],
            "playlist_filled": put.status_code == 200,
            "streams_unbroken": summarize(stream_samples)["errors"] == 0,
        },
        "streams": summarize(stream_samples),
        "resources": sampler.summary(),
    }


def journal(since: str, unit: str = UNIT) -> str | None:
    try:
        done = subprocess.run(
            ["journalctl", "--user", "-u", unit, f"--since={since}", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def established_connections(port: int) -> int | None:
    try:
        done = subprocess.run(
            ["ss", "-Htn", "state", "established", f"( sport = :{port} )"],
            capture_output=True, text=True, timeout=10,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return sum(1 for line in done.stdout.splitlines() if line.strip())


async def health(base_url: str, token: str) -> tuple[int | None, dict | None]:
    # Свой клиент на каждый вызов, без keep-alive: иначе ss видит наше же соединение.
    try:
        async with make_client(base_url, token, timeout=10) as client:
            response = await client.get("/health", headers={"Connection": "close"})
            return response.status_code, response.json()
    except (httpx.HTTPError, ValueError):
        return None, None


async def wait_until_quiet(base_url: str, token: str, wait: float) -> list[str]:
    port = urlparse(base_url).port or 80
    deadline = time.monotonic() + wait
    while True:
        _, body = await health(base_url, token)
        log = journal("-60s")
        reasons = quiet_reasons(
            body, None if log is None else stream_access_lines(log), established_connections(port)
        )
        if not reasons or time.monotonic() > deadline:
            return reasons
        await asyncio.sleep(5)


def restart_service(unit: str = UNIT) -> int:
    return subprocess.run(["systemctl", "--user", "restart", unit], timeout=180).returncode


async def scenario_s7(
    base_url: str, token: str, paths: Paths, manifest: dict, quiet_wait: float, timeout: float
) -> dict:
    """Restart with a probe in the queue; the task must come back and reach done."""
    entry = next(e for e in manifest["probes"] if e["kind"] == "restart")
    reasons = await wait_until_quiet(base_url, token, quiet_wait)
    if reasons:
        return {"skipped": True, "reasons": reasons}
    async with make_client(base_url, token) as client:
        ids = await post_probe(client, paths.manifest, entry)
    write_json(paths.manifest, manifest)
    start = time.monotonic()
    code = restart_service()
    back = None
    while time.monotonic() - start < 120:
        status, body = await health(base_url, token)
        if status == 200 and body and body.get("status") == "healthy":
            back = time.monotonic() - start
            break
        await asyncio.sleep(0.25)
    async with make_client(base_url, token) as client:
        rows = await wait_for_tasks(client, ids, 3.0, timeout)
    apply_task_rows(manifest, rows)
    write_json(paths.manifest, manifest)
    return {
        "restart_exit": code,
        "seconds_to_health": _round(back),
        "task": {k: entry.get(k) for k in ("task_ids", "status", "error_type", "seconds")},
        "checks": {"recovered_to_done": entry.get("status") == "done", "healthy": back is not None},
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _cell(value: Any, width: int) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, float):
        text = f"{value:.1f}"  # формат Python, не локали: всегда точка
    else:
        text = str(value)
    return text.rjust(width)


def table(report: dict) -> str:
    head = (
        f"{'scenario':8} {'label':40} {'n':>6} {'err':>5} "
        f"{'p50':>8} {'p95':>8} {'p99':>8} {'MB':>7} {'ff':>3}"
    )
    lines = [head, "-" * len(head)]
    for name, result in report["scenarios"].items():
        notes: set[str] = set()
        for run in result.get("runs", []):
            res = run.get("resources") or result.get("resources") or {}
            lines.append(
                f"{name:8} {run['label'][:40]:40} {_cell(run.get('n'), 6)} "
                f"{_cell(run.get('errors'), 5)} {_cell(run.get('p50_ms'), 8)} "
                f"{_cell(run.get('p95_ms'), 8)} {_cell(run.get('p99_ms'), 8)} "
                f"{_cell(res.get('memory_peak_mb'), 7)} {_cell(res.get('ffmpeg_max'), 3)}"
            )
            if run.get("note") and run["note"] not in notes:
                notes.add(run["note"])
                lines.append(f"{'':8} ^ {run['note']}")
        for key in ("checks", "skipped", "error"):
            if key in result:
                lines.append(f"{name:8} {key}: {json.dumps(result[key], ensure_ascii=False)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def service_workdir(unit: str = UNIT) -> str | None:
    """The unit's WorkingDirectory, or None when systemd does not say."""
    try:
        done = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "WorkingDirectory", "--value"],
            capture_output=True, text=True, timeout=5,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def checkout_problem(paths: Paths, workdir: str | None) -> str | None:
    """Why this checkout is not the service's; None means it is."""
    hint = f"run it from the service's checkout (systemctl --user show {UNIT} -p WorkingDirectory)"
    if not paths.db.is_file():
        return f"{paths.db} not found: {hint}"
    if workdir and Path(workdir).resolve() != paths.repo.resolve():
        return f"the service runs from {workdir}, not {paths.repo}: {hint}"
    return None


def require_checkout(args: argparse.Namespace, paths: Paths) -> None:
    # Юнит сравнивается только для службы по умолчанию: --base-url может быть другой.
    workdir = service_workdir() if args.base_url == DEFAULT_BASE_URL else None
    if problem := checkout_problem(paths, workdir):
        raise SystemExit(problem)


def parse_scenarios(text: str) -> list[str]:
    chosen = [s.strip().upper() for s in text.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in ALL_SCENARIOS]
    if unknown or not chosen:
        raise SystemExit(f"unknown scenarios {unknown}; choose from {','.join(ALL_SCENARIOS)}")
    return [s for s in ALL_SCENARIOS if s in chosen]


async def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    require_checkout(args, paths)
    scenarios = parse_scenarios(args.scenarios)
    if in_quiet_window(datetime.now()) and not args.ignore_window:
        raise SystemExit("02:45-05:15 belongs to the nightly timers (7.1); run later")
    if "S7" in scenarios and not args.allow_restart:
        raise SystemExit("S7 restarts the service: pass --allow-restart")
    env = read_env(paths.env)
    token = api_token(env)
    hidden = secrets_of(env)
    report: dict[str, Any] = {"started_at": now_iso(), "base_url": args.base_url, "scenarios": {}}
    report_path = args.report or paths.state / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    manifest: dict | None = None
    probing = "S6" in scenarios or "S7" in scenarios

    async with make_client(args.base_url, token) as client:
        rows = await get_library(client)
        if probing:
            if not args.probe_links:
                raise SystemExit("S6/S7 need --probe-links FILE")
            probes, broken = parse_probe_links(Path(args.probe_links).read_text(encoding="utf-8"))
            restart = probes.pop() if "S7" in scenarios else None
            if "S6" in scenarios and not probes:
                raise SystemExit("S6 together with S7 needs at least two probe links")
            log = journal("-2h")
            links = probes + [x for x in (broken, restart) if x]
            live_playlists = None
            if "S6" in scenarios:
                listing = await client.get("/api/playlists")
                listing.raise_for_status()
                live_playlists = [str(p.get("name")) for p in listing.json()]
            problems = probe_problems(
                links,
                None if log is None else rate_limit_lines(log),
                rows,
                [r["url"] or "" for r in task_rows(paths.db)],
                read_json(paths.manifest),
                read_json(paths.snapshot),
                live_playlists,
            )
            if problems:
                print("refusing to probe:\n  " + "\n  ".join(problems), file=sys.stderr)
                return 2
            if "S6" in scenarios:
                manifest = new_manifest(probes, broken, restart)
            else:
                manifest = new_manifest([], None, restart)
            # Манифест — до любого /api/add (7.4, шаг 2).
            write_json(paths.manifest, manifest)

        def save_report() -> None:
            write_json(report_path, report, hidden)

        try:
            covers = sample_paths(rows, 500)
            streams = []
            if "S4" in scenarios or "S6" in scenarios:
                streams = await signed_urls(client, sample_paths(rows, 10, seed=2))
            if "S1" in scenarios:
                report["scenarios"]["S1"] = await scenario_s1(client)
                save_report()
            if "S2" in scenarios:
                report["scenarios"]["S2"] = await scenario_s2(client, rows)
                save_report()
            if "S3" in scenarios or "S4" in scenarios:
                stop = asyncio.Event()
                rounds = None if "S3" in scenarios else 20
                stream_task = asyncio.create_task(
                    stream_loop(client, streams if "S4" in scenarios else [], stop, rounds)
                )
                try:
                    if "S3" in scenarios:
                        warm = await scenario_s3_warm(client, covers)
                        report["scenarios"]["S3"] = {"runs": warm}
                finally:
                    if "S3" in scenarios:
                        stop.set()
                    stream_samples = await stream_task
                if "S4" in scenarios:
                    report["scenarios"]["S4"] = {
                        "runs": [{"label": "stream first byte", **summarize(stream_samples)}]
                    }
                if "S3" in scenarios:
                    with tempfile.TemporaryDirectory(prefix="stress-isolated-") as tmp:
                        cold = await scenario_s3_cold(covers, Path(tmp))
                    report["scenarios"]["S3"]["runs"] += cold
                save_report()
            if "S5" in scenarios:
                report["scenarios"]["S5"] = await scenario_s5(client, args.s5_seconds)
                save_report()
            if "S6" in scenarios and manifest is not None:
                report["scenarios"]["S6"] = await scenario_s6(
                    client, paths, manifest, streams, timeout=args.task_timeout
                )
                save_report()
        except BaseException as exc:
            report["error"] = type(exc).__name__
            if manifest is not None and not args.keep_probes:
                await _cleanup_after(client, paths, manifest, report)
            save_report()
            raise

    try:
        if "S7" in scenarios and manifest is not None:
            report["scenarios"]["S7"] = await scenario_s7(
                args.base_url, token, paths, manifest, args.quiet_wait, args.task_timeout
            )
    finally:
        if manifest is not None and not args.keep_probes:
            async with make_client(args.base_url, token) as client:
                await _cleanup_after(client, paths, manifest, report)
        report["finished_at"] = now_iso()
        write_json(report_path, report, hidden)
    print(table(report))
    print(f"\nreport: {report_path}")
    if manifest is not None:
        print("next: residue (and the Navidrome step of 7.4.6 if it is red)")
    return 0


async def _cleanup_after(
    client: httpx.AsyncClient, paths: Paths, manifest: dict, report: dict
) -> None:
    if report.get("cleanup"):
        return
    errors, purged = await _finish(client, paths, manifest)
    report["cleanup"] = {
        "errors": errors,
        "notes": manifest.get("cleanup_notes", []),
        "navidrome_purged": purged,
    }


def deleted_probe_paths(manifest: dict, snapshot: dict | None) -> set[str]:
    """Result paths the cleanup removed: probes' own tracks, never a pre-existing one."""
    before = set(snapshot["library_paths"]) if snapshot else None
    if before is None:
        return set()
    return {
        e["result_path"]
        for e in manifest.get("probes", [])
        if e.get("deleted")
        and not e.get("navidrome_purged")
        and not e.get("navidrome_never_scanned")
        and e.get("result_path")
        and e["result_path"] not in before
    }


async def finish_cleanup(
    client: httpx.AsyncClient,
    nd_client: httpx.AsyncClient,
    nd: Navidrome | None,
    paths: Paths,
    manifest: dict,
    snapshot: dict | None,
    wait: float = 60.0,
    step: float = 5.0,
    settle: float = 20.0,
) -> tuple[list[str], list[str]]:
    """cleanup, then the Navidrome purge (7.4 steps 5-6); returns (errors, purged).

    The manifest is marked clean only when both went through. A probe path
    left over after ``wait`` passes only if Navidrome has no song for it at
    all (deleted before its scanner got there: ``navidrome_never_scanned``);
    one it still knows, or a failed lookup, is an error.

    The probes' rows and files are removed once more ``settle`` seconds after
    the cleanup: on the live run (08.10) the lyrics search wrote a probe's
    file half a second after the probe was deleted.
    """
    cleaned = time.monotonic()
    errors = await cleanup(client, paths, manifest, snapshot)
    pending = deleted_probe_paths(manifest, snapshot)
    purged: list[str] = []
    if pending and nd is None:
        errors.append(
            "Navidrome credentials are not in adder/.env: purge the probes' missing "
            f"entries by hand (7.4 step 6): {sorted(pending)}"
        )
    elif pending and nd is not None:
        try:
            purged = await purge_navidrome_probes(nd_client, nd, pending, wait, step)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            errors.append(f"Navidrome purge failed ({type(exc).__name__}): see 7.4 step 6")
        never: set[str] = set()
        if left := pending - set(purged):
            # Проба, удалённая до сканера, в «отсутствующих» не появится никогда.
            try:
                never = await navidrome_never_scanned(nd_client, nd, left)
            except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as exc:
                errors.append(
                    f"Navidrome song lookup failed ({type(exc).__name__}): {sorted(left)}"
                )
            else:
                if known := sorted(left - never):
                    errors.append(f"Navidrome still knows these paths, run again: {known}")
        for entry in manifest.get("probes", []):
            if entry.get("result_path") in purged:
                entry["navidrome_purged"] = True
            elif entry.get("result_path") in never:
                entry["navidrome_never_scanned"] = True
    await asyncio.sleep(max(0.0, settle - (time.monotonic() - cleaned)))
    library_before = set(snapshot["library_paths"]) if snapshot else None
    errors += remove_probe_leftovers(paths, manifest, library_before)
    if not errors:
        manifest["cleaned_at"] = now_iso()
    write_json(paths.manifest, manifest)
    return errors, purged


async def _finish(
    client: httpx.AsyncClient, paths: Paths, manifest: dict
) -> tuple[list[str], list[str]]:
    env = read_env(paths.env)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as nd_client:
        return await finish_cleanup(
            client, nd_client, Navidrome.from_env(env), paths, manifest, read_json(paths.snapshot)
        )


async def cmd_snapshot(args: argparse.Namespace, paths: Paths) -> int:
    require_checkout(args, paths)
    if paths.snapshot.is_file() and not args.force:
        raise SystemExit(f"{paths.snapshot} exists; --force to replace it")
    if manifest_open(read_json(paths.manifest)):
        raise SystemExit("probes are not cleaned up yet; a snapshot now would include them")
    env = read_env(paths.env)
    async with (
        make_client(args.base_url, api_token(env)) as client,
        httpx.AsyncClient(timeout=30, trust_env=False) as nd_client,
    ):
        state = await collect_state(client, nd_client, Navidrome.from_env(env), paths)
    write_json(paths.snapshot, state, secrets_of(env))
    print(
        f"snapshot: {len(state['library_paths'])} tracks, {len(state['playlists'])} playlists, "
        f"rows {state['counts']}, Navidrome missing {state['navidrome_missing']}"
    )
    if state["navidrome_missing"] is None:
        print(f"Navidrome missing count unknown: {state['navidrome_note']}")
    return 0


async def cmd_cleanup(args: argparse.Namespace, paths: Paths) -> int:
    require_checkout(args, paths)
    manifest = read_json(paths.manifest)
    if manifest is None:
        print("no probe manifest: nothing to clean")
        return 0
    env = read_env(paths.env)
    async with make_client(args.base_url, api_token(env)) as client:
        errors, purged = await _finish(client, paths, manifest)
    for path in purged:
        print(f"Navidrome: removed missing entry {path}")
    for note in manifest.get("cleanup_notes", []):
        print(f"note: {note}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print("cleanup: done" if not errors else "cleanup: incomplete, run it again")
    return 1 if errors else 0


async def cmd_residue(args: argparse.Namespace, paths: Paths) -> int:
    require_checkout(args, paths)
    snapshot = read_json(paths.snapshot)
    if snapshot is None:
        raise SystemExit("no snapshot to compare with")
    manifest = read_json(paths.manifest)
    env = read_env(paths.env)
    async with (
        make_client(args.base_url, api_token(env)) as client,
        httpx.AsyncClient(timeout=30, trust_env=False) as nd_client,
    ):
        current = await collect_state(
            client,
            nd_client,
            Navidrome.from_env(env),
            paths,
            manifest or {"probes": []},
            set(snapshot["library_paths"]),
        )
    problems = residue_problems(snapshot, current)
    check = current.get("db_snapshot") or {}
    if not check.get("checked"):
        print(f"nightly snapshot not checked: {check.get('reason')}")
    if manifest_open(manifest):
        problems.append("the probe manifest is not marked clean: run `stress.py cleanup`")
    for problem in problems:
        print(f"RESIDUE: {problem}")
    print("residue: clean" if not problems else f"residue: {len(problems)} problem(s)")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="S1-S7 against the service")
    run.add_argument("--scenarios", required=True, help="comma-separated, e.g. S1,S2,S3")
    run.add_argument("--probe-links", help="file with probe links for S6/S7 (see README)")
    run.add_argument("--allow-restart", action="store_true", help="S7 may restart the service")
    run.add_argument("--report", type=Path, help="JSON report path")
    run.add_argument("--s5-seconds", type=float, default=120.0)
    run.add_argument("--task-timeout", type=float, default=900.0)
    run.add_argument("--quiet-wait", type=float, default=600.0, help="S7: wait for no traffic")
    run.add_argument("--keep-probes", action="store_true", help="skip the automatic cleanup")
    run.add_argument("--ignore-window", action="store_true", help="run inside 02:45-05:15")

    snapshot = commands.add_parser("snapshot", help="record the pre-test state")
    snapshot.add_argument("--force", action="store_true")
    commands.add_parser("cleanup", help="remove the probes named in the manifest")
    commands.add_parser("residue", help="compare with the snapshot; exit 1 on leftovers")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths()
    handler = {
        "run": cmd_run,
        "snapshot": cmd_snapshot,
        "cleanup": cmd_cleanup,
        "residue": cmd_residue,
    }[args.command]
    return asyncio.run(handler(args, paths))


if __name__ == "__main__":
    sys.exit(main())
