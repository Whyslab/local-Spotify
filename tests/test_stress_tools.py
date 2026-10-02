"""scripts/stress.py, offline: the probe manifest, cleanup, residue, the numbers,
and the isolated instance used for cold covers.

Nothing here talks to the live service: the HTTP side is httpx.MockTransport,
and the one real server (the isolated instance) runs in this process against a
library in tmp_path.
"""

import asyncio
import importlib.util
import json
import shutil
import sqlite3
import struct
import sys
import threading
import zlib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE_1 = "https://www.youtube.com/watch?v=probe000001"
PROBE_2 = "https://www.youtube.com/watch?v=probe000002"
BROKEN = "https://www.youtube.com/watch?v=broken00000"
UNRELATED = "https://www.youtube.com/watch?v=someoneelse"


def load_stress():
    spec = importlib.util.spec_from_file_location("stress", ROOT / "scripts" / "stress.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses look the module up by name while the class is being built.
    sys.modules["stress"] = module
    spec.loader.exec_module(module)
    return module


stress = load_stress()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """A repository layout in tmp_path, with the service's real database schema."""
    from adder import db, runtime

    found = stress.Paths(repo=tmp_path / "repo")
    found.adder.mkdir(parents=True)
    monkeypatch.setattr(runtime, "DB_PATH", found.db)
    db.db_init()
    # Ни systemctl, ни /proc службы в тестах.
    monkeypatch.setattr(stress, "service_probe", lambda unit="music-adder": None)
    return found


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:8787", transport=httpx.MockTransport(handler)
    )


def sql(db_path: Path, query: str, params=()):
    con = sqlite3.connect(db_path)
    try:
        with con:
            return con.execute(query, params).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Numbers and parsing
# ---------------------------------------------------------------------------


def test_percentile_math():
    assert stress.percentile([], 50) is None
    assert stress.percentile([7.0], 99) == 7.0
    assert stress.percentile([1, 2, 3, 4], 50) == 2.5
    assert stress.percentile([4, 1, 3, 2], 0) == 1
    assert stress.percentile([4, 1, 3, 2], 100) == 4
    # Same as numpy.percentile(range(1, 101), q) with linear interpolation.
    values = list(range(1, 101))
    assert stress.percentile(values, 95) == pytest.approx(95.05)
    assert stress.percentile(values, 99) == pytest.approx(99.01)
    with pytest.raises(ValueError):
        stress.percentile(values, 101)


def test_summary_counts_errors_and_sizes():
    samples = [
        stress.Sample(10.0, 200, 100),
        stress.Sample(20.0, 200, 300),
        stress.Sample(30.0, 503, 0),
        stress.Sample(40.0, None, 0, "ConnectError"),
    ]
    summary = stress.summarize(samples)
    assert summary["n"] == 4
    assert summary["errors"] == 2
    assert summary["server_errors"] == 1
    assert summary["statuses"] == {"200": 2, "503": 1, "ConnectError": 1}
    assert summary["p50_ms"] == 25.0
    assert summary["bytes_mean"] == 200.0


def test_resources_come_from_systemctl_and_proc(tmp_path):
    shown = "MemoryCurrent=104857600\nCPUUsageNSec=[not set]\nMainPID=10\n"
    assert stress.parse_systemctl_show(shown) == {
        "MemoryCurrent": 104857600,
        "CPUUsageNSec": None,
        "MainPID": 10,
    }
    # /proc/10 -> 11 (ffmpeg) -> 12 (ffmpeg); a thread of 10 has 13 (yt-dlp).
    for pid, comm in ((10, "python"), (11, "ffmpeg"), (12, "ffmpeg"), (13, "yt-dlp")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "comm").write_text(comm + "\n")
    for pid, task, children in ((10, 10, "11 "), (10, 20, "13"), (11, 11, "12"), (12, 12, "")):
        folder = tmp_path / str(pid) / "task" / str(task)
        folder.mkdir(parents=True)
        (folder / "children").write_text(children)
    found = stress.descendants(10, proc=tmp_path)
    assert sorted(found) == [(11, "ffmpeg"), (12, "ffmpeg"), (13, "yt-dlp")]


def test_probe_links_file_is_limited():
    probes, broken = stress.parse_probe_links(f"# probes\n{PROBE_1}\n\nbroken: {BROKEN}\n")
    assert probes == [PROBE_1]
    assert broken == BROKEN
    too_many = "\n".join(f"https://youtu.be/vid{n:08d}" for n in range(6))
    with pytest.raises(ValueError, match="at most 5"):
        stress.parse_probe_links(too_many)
    with pytest.raises(ValueError, match="not a YouTube"):
        stress.parse_probe_links("https://example.com/a.mp3")
    with pytest.raises(ValueError, match="twice"):
        stress.parse_probe_links(f"{PROBE_1}\nhttps://youtu.be/probe000001")


def test_probe_preconditions():
    clean = stress.probe_problems([PROBE_1], [], [], [], None, True)
    assert clean == []
    problems = stress.probe_problems(
        [PROBE_1, PROBE_2],
        ["[WARNING] Retry 1/3 after 60s: ERROR: HTTP Error 429: Too Many Requests"],
        [{"path": "A/a.m4a", "source": PROBE_1}],
        [PROBE_2],
        {"cleaned_at": None, "probes": []},
        False,
    )
    text = "\n".join(problems)
    assert "no snapshot" in text
    assert "not cleaned up" in text
    assert "rate limit" in text
    assert "already in the library: probe000001" in text
    assert "already in the tasks table: probe000002" in text
    assert "could not read" in "\n".join(stress.probe_problems([PROBE_1], None, [], [], None, True))


def test_restart_waits_for_quiet():
    assert stress.quiet_reasons({"active_tasks": [{"status": "done"}]}, 0, 0) == []
    reasons = stress.quiet_reasons({"active_tasks": [{"status": "downloading"}]}, 2, 1)
    assert len(reasons) == 3
    log = '127.0.0.1:5 - "GET /api/stream?path=a HTTP/1.1" 206\n"GET /api/stream-url?path=a" 200'
    assert stress.stream_access_lines(log) == 1


def test_report_refuses_a_secret(tmp_path):
    with pytest.raises(RuntimeError, match="secret"):
        stress.write_json(tmp_path / "r.json", {"x": "Bearer s3cret-token"}, ["s3cret-token"])
    assert not (tmp_path / "r.json").exists()


def test_never_touches_plays_player_events_or_pgrep():
    # /api/plays пишет журнал и ListenBrainz, /api/player-event — журнал службы;
    # pgrep -f совпадает с собственной командой (правило хука).
    source = (ROOT / "scripts" / "stress.py").read_text(encoding="utf-8")
    assert '"/api/plays' not in source
    assert "player-event" not in source
    assert '"pgrep' not in source


def test_quiet_window_is_refused():
    from datetime import datetime

    assert stress.in_quiet_window(datetime(2026, 10, 2, 2, 45))
    assert stress.in_quiet_window(datetime(2026, 10, 2, 5, 14))
    assert not stress.in_quiet_window(datetime(2026, 10, 2, 5, 15))
    assert not stress.in_quiet_window(datetime(2026, 10, 2, 2, 44))


def test_cache_names_match_the_service():
    from adder import app, lyrics

    assert stress.THUMB_SIZES == app.THUMB_SIZES
    assert stress.lyrics_key("A/Singles/B.m4a") == lyrics._key("A/Singles/B.m4a").stem


# ---------------------------------------------------------------------------
# S6: the manifest is on disk before /api/add
# ---------------------------------------------------------------------------


def test_manifest_is_written_before_add(paths):
    manifest = stress.new_manifest([PROBE_1, PROBE_2], BROKEN)
    calls = []
    ids = {PROBE_1: 11, PROBE_2: 12, BROKEN: 13}
    seen_links: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/add":
            link = json.loads(request.content)["links"][0]
            on_disk = json.loads(paths.manifest.read_text())
            calls.append(("add", link, link in [e["link"] for e in on_disk["probes"]]))
            if link in seen_links:
                return httpx.Response(200, json={"added": []})  # the repeat is cut off
            seen_links.add(link)
            return httpx.Response(200, json={"added": [ids[link]]})
        if request.method == "POST" and path == "/api/playlists":
            calls.append(("playlist", json.loads(request.content)["name"], True))
            return httpx.Response(200, json={"name": "zz-stress-probe", "revision": "r1"})
        if path == "/api/tasks":
            return httpx.Response(
                200,
                json=[
                    {"id": 11, "status": "done", "result_path": "P/Singles/One.m4a"},
                    {"id": 12, "status": "done", "result_path": "P/Singles/Two.m4a"},
                    {"id": 13, "status": "error", "error_type": "youtube_not_found"},
                ],
            )
        if path == "/api/cover":
            return httpx.Response(200, content=b"cover-of-" + request.url.params["path"].encode())
        if request.method == "PUT":
            calls.append(("fill", json.loads(request.content)["paths"], True))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def go():
        async with client_for(handler) as client:
            return await stress.scenario_s6(client, paths, manifest, [], poll=0.01, timeout=5)

    result = asyncio.run(go())

    adds = [c for c in calls if c[0] == "add"]
    assert [c[1] for c in adds] == [PROBE_1, PROBE_2, BROKEN, PROBE_1]
    assert all(listed for _, _, listed in adds), "an /api/add came before its manifest entry"
    assert calls.index(adds[-1]) < calls.index(("playlist", "zz-stress-probe", True))
    assert ("fill", ["P/Singles/One.m4a", "P/Singles/Two.m4a"], True) in calls

    saved = json.loads(paths.manifest.read_text())
    assert stress.manifest_task_ids(saved) == [11, 12, 13]
    assert stress.manifest_result_paths(saved) == ["P/Singles/One.m4a", "P/Singles/Two.m4a"]
    assert saved["playlist_requested"] is True
    assert saved["probes"][0]["thumb_keys"] == stress.thumb_keys(b"cover-of-P/Singles/One.m4a")
    assert result["checks"]["repeat_not_queued"] is True
    assert result["checks"]["broken_is_youtube_not_found"] is True
    assert result["checks"]["probes_done"] is True


def test_add_is_refused_without_the_manifest_entry(paths):
    stress.write_json(paths.manifest, stress.new_manifest([PROBE_1], None))
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, json={"added": [1]})

    async def go():
        async with client_for(handler) as client:
            await stress.post_probe(client, paths.manifest, {"link": PROBE_2, "kind": "probe"})

    with pytest.raises(RuntimeError, match="manifest"):
        asyncio.run(go())
    assert called == []


# ---------------------------------------------------------------------------
# Cleanup touches exactly what the manifest names
# ---------------------------------------------------------------------------


def test_cleanup_deletes_exactly_the_manifest_paths(paths, tmp_path):
    library = tmp_path / "library"
    for rel in ("Probe/Singles/One.m4a", "Old/Singles/Kept.m4a", "Other/Singles/X.m4a"):
        (library / rel).parent.mkdir(parents=True, exist_ok=True)
        (library / rel).write_bytes(b"audio")
    snapshot = {"library_paths": ["Old/Singles/Kept.m4a", "Other/Singles/X.m4a"]}
    manifest = stress.new_manifest([PROBE_1, PROBE_2], BROKEN)
    manifest["playlist_requested"] = True
    # PROBE_2 turned out a duplicate: its task points at a track from before the test.
    for entry, tid in zip(manifest["probes"], (11, 12, 13), strict=True):
        entry["task_ids"] = [tid]
    stress.write_json(paths.manifest, manifest)

    for tid, url, status, result in (
        (11, PROBE_1, "done", "Probe/Singles/One.m4a"),
        (12, PROBE_2, "done", "Old/Singles/Kept.m4a"),
        (13, BROKEN, "error", None),
        (99, UNRELATED, "done", "Other/Singles/X.m4a"),
    ):
        sql(
            paths.db,
            "INSERT INTO tasks(id, url, status, result_path) VALUES(?, ?, ?, ?)",
            (tid, url, status, result),
        )
    for path in ("Probe/Singles/One.m4a", "Old/Singles/Kept.m4a"):
        sql(paths.db, "INSERT INTO audio_features(path, sha256) VALUES(?, 'x')", (path,))
    sql(paths.db, "INSERT INTO plays(path, played_at) VALUES('Old/Singles/Kept.m4a', 'now')")

    (paths.trash / "Unrelated").mkdir(parents=True)
    (paths.trash / "Unrelated" / "keep.m4a").write_bytes(b"old trash")
    paths.thumbs.mkdir(parents=True)
    probe_thumbs = [paths.thumbs / f"{k}.jpg" for k in stress.thumb_keys(b"probe-cover")]
    for thumb in probe_thumbs:
        thumb.write_bytes(b"jpg")
    (paths.thumbs / "unrelated.jpg").write_bytes(b"jpg")
    paths.lyrics.mkdir(parents=True)
    probe_lyrics = paths.lyrics / f"{stress.lyrics_key('Probe/Singles/One.m4a')}.json"
    probe_lyrics.write_text("{}")
    kept_lyrics = paths.lyrics / f"{stress.lyrics_key('Old/Singles/Kept.m4a')}.json"
    kept_lyrics.write_text("{}")

    probe_history = paths.playlist_history / "zz-stress-probe"
    probe_history.mkdir(parents=True)
    (probe_history / "1.m3u").write_text("#EXTM3U\n")
    (paths.playlist_history / "Monday").mkdir()
    (paths.playlist_history / "Monday" / "1.m3u").write_text("#EXTM3U\n")

    deleted, playlist_deletes = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tasks":
            rows = sql(paths.db, "SELECT id, url, status, result_path FROM tasks")
            keys = ("id", "url", "status", "result_path")
            return httpx.Response(200, json=[dict(zip(keys, r, strict=True)) for r in rows])
        if path == "/api/cover":
            return httpx.Response(200, content=b"probe-cover")
        if request.method == "DELETE" and path == "/api/library":
            rel = json.loads(request.content)["path"]
            deleted.append(rel)
            target = paths.trash / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(library / rel, target)
            return httpx.Response(200, json={"deleted": rel, "trash": str(target)})
        if request.method == "DELETE" and path.startswith("/api/playlists/"):
            playlist_deletes.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def go():
        async with client_for(handler) as client:
            return await stress.cleanup(client, paths, manifest, snapshot)

    errors = asyncio.run(go())

    assert errors == []
    assert deleted == ["Probe/Singles/One.m4a"]
    assert playlist_deletes == ["zz-stress-probe"]
    assert (library / "Old/Singles/Kept.m4a").exists()
    assert (library / "Other/Singles/X.m4a").exists()
    assert not (paths.trash / "Probe").exists(), "the probe's file and empty folders stay in trash"
    assert (paths.trash / "Unrelated" / "keep.m4a").exists()
    assert sorted(r[0] for r in sql(paths.db, "SELECT id FROM tasks")) == [99]
    assert sql(paths.db, "SELECT path FROM audio_features") == [("Old/Singles/Kept.m4a",)]
    assert sql(paths.db, "SELECT path FROM plays") == [("Old/Singles/Kept.m4a",)]
    assert not any(t.exists() for t in probe_thumbs)
    assert (paths.thumbs / "unrelated.jpg").exists()
    assert not probe_lyrics.exists()
    assert kept_lyrics.exists()
    saved = json.loads(paths.manifest.read_text())
    assert saved["cleaned_at"]
    assert saved["probes"][1]["kept_existing"] is True
    assert not probe_history.exists(), "the probe playlist's saved versions go too"
    assert (paths.playlist_history / "Monday" / "1.m3u").exists()


def test_cleanup_without_snapshot_deletes_no_track(paths):
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["probes"][0].update(task_ids=[11], result_path="Probe/Singles/One.m4a")
    stress.write_json(paths.manifest, manifest)
    sql(paths.db, "INSERT INTO audio_features(path, sha256) VALUES('Probe/Singles/One.m4a', 'x')")
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=[])

    async def go():
        async with client_for(handler) as client:
            return await stress.cleanup(client, paths, manifest, None)

    errors = asyncio.run(go())
    assert any("no snapshot" in e for e in errors)
    assert ("DELETE", "/api/library") not in requests
    assert sql(paths.db, "SELECT COUNT(*) FROM audio_features") == [(1,)]
    assert json.loads(paths.manifest.read_text())["cleaned_at"] is None


def test_trash_removal_stays_inside_trash(tmp_path):
    trash = tmp_path / "trash"
    trash.mkdir()
    outside = tmp_path / "elsewhere.m4a"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        stress.remove_trash_file(trash / ".." / "elsewhere.m4a", trash)
    assert outside.exists()


# ---------------------------------------------------------------------------
# Residue
# ---------------------------------------------------------------------------


def residue_handler(library_paths, missing):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.port == 8787:
            assert request.headers.get("authorization") == "Bearer t"
        else:
            assert "authorization" not in request.headers, "service token sent to Navidrome"
        if path == "/api/library":
            return httpx.Response(200, json=[{"path": p} for p in library_paths])
        if request.url.port == 8787 and path == "/api/playlists":
            return httpx.Response(200, json=[{"name": "Mix", "revision": "r1"}])
        if path == "/auth/login":
            return httpx.Response(200, json={"token": "nd"})
        if path == "/api/missing":
            if missing is None:
                return httpx.Response(500)
            return httpx.Response(200, json=[], headers={"X-Total-Count": str(missing)})
        if path == "/api/playlist":
            return httpx.Response(200, json=[{"name": "Mix"}])
        return httpx.Response(404)

    return handler


def state_of(paths, library_paths, missing, manifest=None, before=None):
    handler = residue_handler(library_paths, missing)
    nd = stress.Navidrome("http://127.0.0.1:4533", "admin", "pw")

    async def go():
        transport = httpx.MockTransport(handler)
        async with (
            httpx.AsyncClient(
                base_url="http://127.0.0.1:8787",
                headers={"Authorization": "Bearer t"},
                transport=transport,
            ) as client,
            httpx.AsyncClient(transport=transport) as nd_client,
        ):
            return await stress.collect_state(client, nd_client, nd, paths, manifest, before)

    return asyncio.run(go())


def test_residue_passes_when_clean(paths):
    snapshot = state_of(paths, ["A/Singles/a.m4a"], 3)
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["probes"][0].update(task_ids=[11], result_path="Probe/Singles/One.m4a")
    current = state_of(paths, ["A/Singles/a.m4a"], 3, manifest, set(snapshot["library_paths"]))
    assert stress.residue_problems(snapshot, current) == []


def test_residue_flags_track_task_row_and_missing_increase(paths):
    snapshot = state_of(paths, ["A/Singles/a.m4a"], 3)
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["probes"][0].update(task_ids=[11], result_path="Probe/Singles/One.m4a")
    sql(
        paths.db,
        "INSERT INTO tasks(id, url, status, result_path) VALUES(11, ?, 'done', ?)",
        (PROBE_1, "Probe/Singles/One.m4a"),
    )
    current = state_of(
        paths,
        ["A/Singles/a.m4a", "Probe/Singles/One.m4a"],
        4,
        manifest,
        set(snapshot["library_paths"]),
    )
    problems = "\n".join(stress.residue_problems(snapshot, current))
    assert "Probe/Singles/One.m4a" in problems
    assert "table tasks: 0 rows in the snapshot, 1 now" in problems
    assert "table tasks: 1 row(s) of the probes" in problems
    assert "Navidrome missing files: 3 before, 4 now" in problems


def test_residue_is_red_when_navidrome_count_is_unknown(paths):
    snapshot = state_of(paths, ["A/Singles/a.m4a"], 3)
    current = state_of(paths, ["A/Singles/a.m4a"], None, {"probes": []}, set())
    problems = stress.residue_problems(snapshot, current)
    assert len(problems) == 1
    assert "unknown" in problems[0]
    assert "check by hand" in problems[0]


# ---------------------------------------------------------------------------
# The isolated instance for cold covers
# ---------------------------------------------------------------------------


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _mtime(path: Path):
    return path.stat().st_mtime_ns if path.exists() else None


def test_isolated_instance_starts_no_threads(tmp_path, monkeypatch):
    from mutagen.mp4 import MP4, MP4Cover

    from adder import config, covers, lyrics, playlists, runtime

    library = tmp_path / "fixture-library"
    track = library / "Band" / "Singles" / "Song.m4a"
    track.parent.mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "fixtures" / "tone.m4a", track)
    tags = MP4(track)
    tags["covr"] = [MP4Cover(_png(), imageformat=MP4Cover.FORMAT_PNG)]
    tags.save()
    monkeypatch.setattr(config, "LIBRARY", library)

    watched = [ROOT / "adder", ROOT / "trash", library, track.parent]
    mtimes = {p: _mtime(p) for p in watched}
    copies = {
        "lyrics.CACHE_DIR": lambda: lyrics.CACHE_DIR,
        "covers.COVERS_DIR": lambda: covers.COVERS_DIR,
        "playlists.HISTORY_DIR": lambda: playlists.HISTORY_DIR,
    }
    before_paths = {name: getattr(runtime, name) for name in stress.RUNTIME_PATHS}
    before_copies = {name: read() for name, read in copies.items()}
    before_threads = {t.ident for t in threading.enumerate()}
    root = tmp_path / "isolated"

    with stress.IsolatedInstance(root) as instance:
        for name in stress.RUNTIME_PATHS:
            assert getattr(runtime, name).is_relative_to(root), name
        for name, read in copies.items():
            assert read().is_relative_to(root), name
        assert config.NAVIDROME_USER == ""

        with httpx.Client(base_url=instance.url, trust_env=False, timeout=30) as client:
            auth = {"Authorization": f"Bearer {instance.token}"}
            cover = client.get(
                "/api/cover", params={"path": "Band/Singles/Song.m4a", "size": 96}, headers=auth
            )
            refused = client.get("/api/cover", params={"path": "Band/Singles/Song.m4a"})
        assert cover.status_code == 200
        assert refused.status_code == 401
        if shutil.which("ffmpeg"):
            assert list((root / "thumb-cache").glob("*.jpg")), "thumbnail not in the temp dir"

        started = [t for t in threading.enumerate() if t.ident not in before_threads]
        names = {t.name for t in started}
        assert stress.SERVER_THREAD in names
        # Только сервер и пул запросов anyio: ни воркеров, ни синхронизации,
        # ни текстов, ни ListenBrainz, ни «Артиста целиком».
        assert names <= {stress.SERVER_THREAD, "AnyIO worker thread"}, names

    assert not any(t.name == stress.SERVER_THREAD for t in threading.enumerate())
    assert {name: getattr(runtime, name) for name in stress.RUNTIME_PATHS} == before_paths
    assert {name: read() for name, read in copies.items()} == before_copies
    assert library == config.LIBRARY
    assert {p: _mtime(p) for p in watched} == mtimes


def test_range_offsets_stay_inside_the_file():
    rng = stress.random.Random(0)
    size = 1_489_054  # MAYOT/Singles/430.m4a: a 50-second track
    for _ in range(2000):
        offset = stress.range_offset(rng, size)
        assert offset >= 0 and offset + 65535 < size
    assert stress.range_offset(rng, 1000) == 0
    assert stress.range_offset(rng, None) == 0


def test_file_size_is_read_from_content_range():
    def handler(request):
        assert request.headers["range"] == "bytes=0-0"
        return httpx.Response(206, headers={"Content-Range": "bytes 0-0/1489054"}, content=b"x")

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://t"
        ) as c:
            return await stress.file_size(c, "/api/stream?path=a")

    assert asyncio.run(go()) == 1489054


def test_leftover_probe_playlist_history_is_flagged(paths):
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["playlist_requested"] = True
    (paths.playlist_history / "zz-stress-probe").mkdir(parents=True)
    left = stress.leftover_files(paths, manifest, set())
    assert left == [str(paths.playlist_history / "zz-stress-probe")]


def test_navidrome_purge_removes_only_the_probes_missing_entries():
    nd = stress.Navidrome("http://nd", "admin", "pw")
    missing = [
        {"id": "p1", "path": "Kevin MacLeod/Singles/Outback Call.m4a"},
        {"id": "old", "path": "Baby Melo/Singles/Slappy Tap.m4a"},
    ]
    deleted: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "t"})
        assert request.headers["x-nd-authorization"] == "Bearer t"
        if request.method == "GET" and request.url.path == "/api/missing":
            gone = {i for batch in deleted for i in batch}
            return httpx.Response(200, json=[m for m in missing if m["id"] not in gone])
        if request.method == "DELETE" and request.url.path == "/api/missing":
            deleted.append(request.url.params.get_list("id"))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await stress.purge_navidrome_probes(
                client, nd, {"Kevin MacLeod/Singles/Outback Call.m4a"}, wait=0, step=0
            )

    assert asyncio.run(go()) == ["Kevin MacLeod/Singles/Outback Call.m4a"]
    assert deleted == [["p1"]], "an entry that was missing before the test is left alone"


def test_purge_targets_only_deleted_probes_that_were_not_there_before():
    manifest = stress.new_manifest([PROBE_1, PROBE_2], None)
    manifest["probes"][0].update(result_path="P/Singles/One.m4a", deleted=True)
    manifest["probes"][1].update(result_path="Old/Singles/Kept.m4a", deleted=True)
    snapshot = {"library_paths": ["Old/Singles/Kept.m4a"]}
    assert stress.deleted_probe_paths(manifest, snapshot) == {"P/Singles/One.m4a"}
    assert stress.deleted_probe_paths(manifest, None) == set()
