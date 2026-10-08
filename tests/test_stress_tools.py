"""scripts/stress.py, offline: the probe manifest, cleanup, residue, the numbers,
and the isolated instance used for cold covers.

Nothing here talks to the live service: the HTTP side is httpx.MockTransport,
and the one real server (the isolated instance) runs in this process against a
library in tmp_path.
"""

import asyncio
import importlib
import importlib.util
import json
import shutil
import sqlite3
import struct
import sys
import threading
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE_1 = "https://www.youtube.com/watch?v=probe000001"
PROBE_2 = "https://www.youtube.com/watch?v=probe000002"
BROKEN = "https://www.youtube.com/watch?v=broken00000"
PROBE_3 = "https://www.youtube.com/watch?v=probe000003"
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


def fresh_snapshot(library_paths=(), playlists=None, age=timedelta(minutes=5)):
    taken = datetime.now().astimezone() - age
    return {
        "taken_at": taken.isoformat(timespec="seconds"),
        "library_paths": list(library_paths),
        "playlists": playlists or {},
    }


def test_probe_preconditions():
    clean = stress.probe_problems([PROBE_1], [], [], [], None, fresh_snapshot())
    assert clean == []
    problems = stress.probe_problems(
        [PROBE_1, PROBE_2],
        ["[WARNING] Retry 1/3 after 60s: ERROR: HTTP Error 429: Too Many Requests"],
        [{"path": "A/a.m4a", "source": PROBE_1}],
        [PROBE_2],
        {"cleaned_at": None, "probes": []},
        None,
    )
    text = "\n".join(problems)
    assert "no snapshot" in text
    assert "not cleaned up" in text
    assert "rate limit" in text
    assert "already in the library: probe000001" in text
    assert "already in the tasks table: probe000002" in text
    unread = stress.probe_problems([PROBE_1], None, [], [], None, fresh_snapshot())
    assert "could not read" in "\n".join(unread)


def test_probes_refuse_a_stale_or_outdated_snapshot():
    rows = [{"path": "A/a.m4a", "source": ""}]

    def problems(snapshot):
        return "\n".join(stress.probe_problems([PROBE_1], [], rows, [], None, snapshot))

    assert problems(fresh_snapshot(["A/a.m4a"])) == ""
    assert "older than 2 h" in problems(fresh_snapshot(["A/a.m4a"], age=timedelta(hours=2.1)))
    undated = {k: v for k, v in fresh_snapshot(["A/a.m4a"]).items() if k != "taken_at"}
    assert "taken_at" in problems(undated)
    naive = fresh_snapshot(["A/a.m4a"])
    naive["taken_at"] = datetime.now().isoformat(timespec="seconds")  # без пояса — местное
    assert problems(naive) == ""
    changed = problems(fresh_snapshot(["A/a.m4a", "B/b.m4a"]))
    assert "library changed since the snapshot" in changed
    assert "snapshot --force" in changed


def test_s6_refuses_an_existing_probe_playlist():
    def problems(snapshot, live):
        return "\n".join(stress.probe_problems([PROBE_1], [], [], [], None, snapshot, live))

    assert problems(fresh_snapshot(), ["Mix"]) == ""
    in_snapshot = fresh_snapshot(playlists={stress.PROBE_PLAYLIST: "r1"})
    assert stress.PROBE_PLAYLIST in problems(in_snapshot, ["Mix"])
    assert stress.PROBE_PLAYLIST in problems(fresh_snapshot(), ["Mix", stress.PROBE_PLAYLIST])


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
    from adder import lyrics, thumbs

    assert stress.THUMB_SIZES == thumbs.SIZES
    assert stress.thumb_keys(b"art") == [thumbs._key(b"art", size) for size in thumbs.SIZES]
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
        if request.method == "GET" and path == "/api/library":
            sources = {"Probe/Singles/One.m4a": PROBE_1, "Old/Singles/Kept.m4a": PROBE_2}
            listed = [p for p in sources if (library / p).exists()]
            return httpx.Response(200, json=[{"path": p, "source": sources[p]} for p in listed])
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
    # Чистой уборку объявляет finish_cleanup, после Navidrome.
    assert saved["cleaned_at"] is None
    assert saved["probes"][0]["deleted"] is True
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


def residue_handler(library_paths, missing, pending=0, sync_rows=()):
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
        if path == "/health":
            if pending is None:
                return httpx.Response(500)
            return httpx.Response(200, json={"status": "healthy", "navidrome_pending": pending})
        if path == "/api/sync":
            result = {"navidrome": "ok", "playlists": list(sync_rows)}
            return httpx.Response(200, json={"queued": pending, "last_result": result})
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


def state_of(paths, library_paths, missing, manifest=None, before=None, **service):
    handler = residue_handler(library_paths, missing, **service)
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


def test_residue_flags_navidrome_queue_and_probe_sync(paths):
    snapshot = state_of(paths, ["A/Singles/a.m4a"], 3)
    before = set(snapshot["library_paths"])
    clean_sync = [{"playlist": "Mix", "reason": "updated on the phone"}]
    clean = state_of(paths, ["A/Singles/a.m4a"], 3, {"probes": []}, before, sync_rows=clean_sync)
    assert stress.residue_problems(snapshot, clean) == []

    probe_sync = [{"playlist": stress.PROBE_PLAYLIST, "reason": "updated on the phone"}]
    busy = state_of(
        paths, ["A/Singles/a.m4a"], 3, {"probes": []}, before, pending=2, sync_rows=probe_sync
    )
    problems = "\n".join(stress.residue_problems(snapshot, busy))
    assert "navidrome_pending is 2" in problems
    assert f"/api/sync: {stress.PROBE_PLAYLIST!r}" in problems

    unknown = state_of(paths, ["A/Singles/a.m4a"], 3, {"probes": []}, before, pending=None)
    assert "navidrome_pending unknown" in "\n".join(stress.residue_problems(snapshot, unknown))


def test_residue_is_red_when_navidrome_count_is_unknown(paths):
    snapshot = state_of(paths, ["A/Singles/a.m4a"], 3)
    current = state_of(paths, ["A/Singles/a.m4a"], None, {"probes": []}, set())
    problems = stress.residue_problems(snapshot, current)
    assert len(problems) == 1
    assert "unknown" in problems[0]
    assert "check by hand" in problems[0]


def test_nightly_snapshot_check_finds_probe_and_leaves_no_files(paths):
    """backup.sh copies adder.db in WAL mode; reading the copy must not write next to it."""
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["probes"][0].update(task_ids=[11], result_path="Probe/Singles/One.m4a")
    manifest["cleaned_at"] = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
    sql(paths.db, "INSERT INTO tasks(id, url, status) VALUES(11, ?, 'done')", (PROBE_1,))
    paths.db_snapshots.mkdir()
    nightly = paths.db_snapshots / "adder_20261003_030600.db"
    source, copy = sqlite3.connect(paths.db), sqlite3.connect(nightly)
    try:
        source.backup(copy)
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        source.close()
        copy.close()
    nightly.with_name(nightly.name + "-shm").unlink(missing_ok=True)
    nightly.with_name(nightly.name + "-wal").unlink(missing_ok=True)

    check = stress.db_snapshot_check(paths, manifest, set())
    assert check["checked"] is True
    assert check["rows"]["tasks"] == 1
    assert sorted(p.name for p in paths.db_snapshots.iterdir()) == [nightly.name]


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


def _tree(root: Path) -> dict[str, int]:
    """Every file and folder under ``root`` (with ``root``) and its mtime."""
    if not root.exists():
        return {}
    found = {".": root.stat().st_mtime_ns}
    found.update({str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*")})
    return found


def test_isolated_instance_starts_no_threads(tmp_path, monkeypatch):
    from mutagen.mp4 import MP4, MP4Cover

    from adder import config, db, runtime

    library = tmp_path / "fixture-library"
    track = library / "Band" / "Singles" / "Song.m4a"
    track.parent.mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "fixtures" / "tone.m4a", track)
    tags = MP4(track)
    tags["covr"] = [MP4Cover(_png(), imageformat=MP4Cover.FORMAT_PNG)]
    tags.save()
    monkeypatch.setattr(config, "LIBRARY", library)

    # «Живые» пути службы — в tmp, а не ROOT/adder: там настоящая служба пишет
    # журнал SQLite, и время изменения папки плавало само по себе.
    live = tmp_path / "live-adder"
    for name, relative in stress.RUNTIME_PATHS.items():
        monkeypatch.setattr(runtime, name, live / relative)
    modules = {}
    for (module, name), relative in stress.MODULE_COPIES.items():
        modules[module, name] = importlib.import_module(f"adder.{module}")
        monkeypatch.setattr(modules[module, name], name, live / relative)
    live.mkdir()
    db.db_init()
    for relative in ("thumb-cache", "lyrics-cache", "trash", "playlist-history/Monday"):
        (live / relative).mkdir(parents=True)
        (live / relative / "old").write_bytes(b"live")
    (live / "artist-import.json").write_text("[]")
    monkeypatch.setattr(config, "NAVIDROME_USER", "someone")
    monkeypatch.setattr(config, "NAVIDROME_PASSWORD", "pw")

    def where() -> dict[str, Path]:
        found = {name: getattr(runtime, name) for name in stress.RUNTIME_PATHS}
        found.update({f"{m}.{n}": getattr(modules[m, n], n) for m, n in stress.MODULE_COPIES})
        return found

    before_paths = where()
    assert all(p.is_relative_to(live) for p in before_paths.values())
    before_trees = {"live": _tree(live), "library": _tree(library)}
    before_threads = {t.ident for t in threading.enumerate()}
    root = tmp_path / "isolated"

    with stress.IsolatedInstance(root) as instance:
        for name, path in where().items():
            assert path.is_relative_to(root), name
        assert (config.NAVIDROME_USER, config.NAVIDROME_PASSWORD) == ("", "")

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
        instance.clear_thumbs()
        assert not (root / "thumb-cache").exists()

        started = [t for t in threading.enumerate() if t.ident not in before_threads]
        names = {t.name for t in started}
        assert stress.SERVER_THREAD in names
        # Только сервер и пул запросов anyio: ни воркеров, ни синхронизации,
        # ни текстов, ни ListenBrainz, ни «Артиста целиком».
        assert names <= {stress.SERVER_THREAD, "AnyIO worker thread"}, names

    assert not any(t.name == stress.SERVER_THREAD for t in threading.enumerate())
    assert where() == before_paths
    assert (config.NAVIDROME_USER, config.NAVIDROME_PASSWORD) == ("someone", "pw")
    assert library == config.LIBRARY
    assert {"live": _tree(live), "library": _tree(library)} == before_trees


def test_clear_thumbs_stays_inside_the_instance(tmp_path, monkeypatch):
    from adder import runtime

    live_thumbs = tmp_path / "live-thumbs"
    live_thumbs.mkdir()
    (live_thumbs / "keep.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(runtime, "THUMB_DIR", live_thumbs)
    instance = stress.IsolatedInstance(tmp_path / "isolated")
    with pytest.raises(RuntimeError, match="not inside"):
        instance.clear_thumbs()
    assert (live_thumbs / "keep.jpg").exists()


def test_exit_keeps_temp_paths_while_the_server_still_runs(tmp_path):
    from types import SimpleNamespace

    temp_thumbs = tmp_path / "isolated" / "thumb-cache"
    owner = SimpleNamespace(THUMB_DIR=temp_thumbs)
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, args=(5,), daemon=True)
    stuck.start()
    # Старый код ждал 15 с и всё равно возвращал пути; таймер не даёт ему висеть.
    timer = threading.Timer(0.5, release.set)
    timer.start()
    instance = stress.IsolatedInstance(tmp_path / "isolated")
    instance.join_timeout = 0.05
    instance._server = SimpleNamespace(should_exit=False)
    instance._thread = stuck
    instance._saved = [(owner, "THUMB_DIR", tmp_path / "live" / "thumb-cache")]
    try:
        with pytest.raises(RuntimeError, match="still running"):
            instance.__exit__(None, None, None)
        assert owner.THUMB_DIR is temp_thumbs, "live path restored under a running server"
    finally:
        release.set()
        timer.cancel()
        stuck.join()


def test_cold_cover_numbers_are_marked_relative(tmp_path):
    runs = asyncio.run(stress.scenario_s3_cold([], tmp_path / "isolated"))
    assert runs
    assert all("same process" in run["note"] for run in runs)
    text = stress.table({"scenarios": {"S3": {"runs": runs}}})
    assert "same process" in text


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


# ---------------------------------------------------------------------------
# Review hardening: what cleanup may delete, and where the tool may run
# ---------------------------------------------------------------------------


def test_cleanup_deletes_only_a_track_the_probe_owns(paths):
    manifest = stress.new_manifest([PROBE_1, PROBE_2, PROBE_3], None)
    for entry, tid in zip(manifest["probes"], (11, 12, 13), strict=True):
        entry["task_ids"] = [tid]
    stress.write_json(paths.manifest, manifest)
    # 12: the row's link is someone else's; 13: the file's source is someone else's.
    for tid, url, result in (
        (11, PROBE_1, "Probe/Singles/One.m4a"),
        (12, UNRELATED, "Swapped/Singles/Two.m4a"),
        (13, PROBE_3, "Other/Singles/Three.m4a"),
    ):
        sql(
            paths.db,
            "INSERT INTO tasks(id, url, status, result_path) VALUES(?, ?, 'done', ?)",
            (tid, url, result),
        )
    library = {
        "Probe/Singles/One.m4a": PROBE_1,
        "Swapped/Singles/Two.m4a": PROBE_2,
        "Other/Singles/Three.m4a": UNRELATED,
    }
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tasks":
            rows = sql(paths.db, "SELECT id, url, status, result_path FROM tasks")
            keys = ("id", "url", "status", "result_path")
            return httpx.Response(200, json=[dict(zip(keys, r, strict=True)) for r in rows])
        if request.method == "GET" and path == "/api/library":
            return httpx.Response(200, json=[{"path": p, "source": s} for p, s in library.items()])
        if request.method == "DELETE" and path == "/api/library":
            deleted.append(json.loads(request.content)["path"])
            return httpx.Response(200, json={"trash": None})
        return httpx.Response(404)

    async def go():
        async with client_for(handler) as client:
            return await stress.cleanup(client, paths, manifest, {"library_paths": []})

    errors = "\n".join(asyncio.run(go()))
    assert deleted == ["Probe/Singles/One.m4a"]
    assert "Swapped/Singles/Two.m4a" in errors
    assert "Other/Singles/Three.m4a" in errors
    # Строка задачи — улика: пока трек не убран, она остаётся.
    assert sql(paths.db, "SELECT id FROM tasks ORDER BY id") == [(12,), (13,)]


def test_cleanup_never_deletes_a_playlist_the_manifest_renames(paths):
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["playlist"] = "Monday"
    manifest["playlist_requested"] = True
    stress.write_json(paths.manifest, manifest)
    (paths.playlist_history / "Monday").mkdir(parents=True)
    (paths.playlist_history / "Monday" / "1.m3u").write_text("#EXTM3U\n")
    playlist_deletes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.startswith("/api/playlists/"):
            playlist_deletes.append(request.url.path)
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[])

    async def go():
        async with client_for(handler) as client:
            return await stress.cleanup(client, paths, manifest, {"library_paths": []})

    errors = asyncio.run(go())
    assert playlist_deletes == []
    assert (paths.playlist_history / "Monday" / "1.m3u").exists()
    assert any("Monday" in e for e in errors)

    # Манифест, где удаление уже отмечено: история чужой подборки всё равно цела.
    manifest["playlist_deleted"] = True
    asyncio.run(go())
    assert (paths.playlist_history / "Monday" / "1.m3u").exists()


def test_cleanup_ignores_thumb_keys_that_are_not_sha1_names(paths):
    good = stress.thumb_keys(b"probe-cover")[0]
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["probes"][0].update(
        result_path="Probe/Singles/One.m4a", deleted=True, thumb_keys=["../escape", good]
    )
    stress.write_json(paths.manifest, manifest)
    paths.thumbs.mkdir(parents=True)
    (paths.thumbs / f"{good}.jpg").write_bytes(b"jpg")
    escape = paths.adder / "escape.jpg"
    escape.write_bytes(b"not a thumbnail")
    assert str(escape) not in stress.leftover_files(paths, manifest, set())
    assert stress.valid_thumb_keys({"thumb_keys": None}) == ([], [])

    async def go():
        async with client_for(lambda r: httpx.Response(200, json=[])) as client:
            return await stress.cleanup(client, paths, manifest, {"library_paths": []})

    errors = asyncio.run(go())
    assert escape.exists()
    assert not (paths.thumbs / f"{good}.jpg").exists()
    assert any("thumb key" in e for e in errors)


def test_probe_playlist_is_ours_only_once_the_post_succeeded(paths):
    manifest = stress.new_manifest([PROBE_1], None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/add":
            return httpx.Response(200, json={"added": [11]})
        if request.method == "POST" and request.url.path == "/api/playlists":
            return httpx.Response(409, json={"detail": "Playlist already exists"})
        return httpx.Response(404)

    async def go():
        async with client_for(handler) as client:
            await stress.scenario_s6(client, paths, manifest, [], poll=0.01, timeout=1)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())
    assert json.loads(paths.manifest.read_text())["playlist_requested"] is False


def test_cleanup_is_marked_clean_only_after_navidrome_forgot_the_probes(paths):
    manifest = stress.new_manifest([PROBE_1], None)
    manifest["probes"][0].update(task_ids=[11], result_path="P/Singles/One.m4a", deleted=True)
    stress.write_json(paths.manifest, manifest)
    snapshot = {"library_paths": []}
    nd = stress.Navidrome("http://nd", "admin", "pw")
    scanned = []

    def service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    def navidrome(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "t"})
        if request.method == "GET" and request.url.path == "/api/song":
            # Сканер удаления ещё не видел: для Navidrome это живая песня.
            live = [] if scanned else [{"path": "P/Singles/One.m4a", "missing": False}]
            return httpx.Response(200, json=live)
        if request.method == "GET" and request.url.path == "/api/missing":
            return httpx.Response(200, json=[{"id": "p1", "path": p} for p in scanned])
        if request.method == "DELETE" and request.url.path == "/api/missing":
            scanned.clear()
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def go(nd_):
        async with (
            client_for(service) as client,
            httpx.AsyncClient(transport=httpx.MockTransport(navidrome)) as nd_client,
        ):
            return await stress.finish_cleanup(
                client, nd_client, nd_, paths, manifest, snapshot, wait=0, step=0
            )

    def saved():
        return json.loads(paths.manifest.read_text())

    errors, purged = asyncio.run(go(None))
    assert any("Navidrome credentials" in e for e in errors)
    assert saved()["cleaned_at"] is None

    # Сканер ещё не заметил удаления: «отсутствующего» нет, уборка не окончена.
    errors, purged = asyncio.run(go(nd))
    assert purged == []
    assert any("P/Singles/One.m4a" in e for e in errors)
    assert saved()["cleaned_at"] is None

    scanned.append("P/Singles/One.m4a")
    errors, purged = asyncio.run(go(nd))
    assert (errors, purged) == ([], ["P/Singles/One.m4a"])
    assert saved()["cleaned_at"]
    assert saved()["probes"][0]["navidrome_purged"] is True


@pytest.mark.parametrize("command", ["run", "snapshot", "cleanup", "residue"])
def test_commands_refuse_a_checkout_without_the_service_database(tmp_path, monkeypatch, command):
    other = stress.Paths(repo=tmp_path / "checkout")
    other.adder.mkdir(parents=True)
    monkeypatch.setattr(stress, "service_workdir", lambda unit="music-adder": None, raising=False)
    # Порт, где никого нет: ни одна команда не должна дойти до живой службы.
    argv = ["--base-url", "http://127.0.0.1:9", command]
    if command == "run":
        argv += ["--scenarios", "S1", "--ignore-window"]
    args = stress.build_parser().parse_args(argv)
    handler = {
        "run": stress.cmd_run,
        "snapshot": stress.cmd_snapshot,
        "cleanup": stress.cmd_cleanup,
        "residue": stress.cmd_residue,
    }[command]
    with pytest.raises(SystemExit, match="WorkingDirectory"):
        asyncio.run(handler(args, other))


def test_checkout_must_be_the_one_the_service_runs_from(paths, tmp_path):
    assert stress.checkout_problem(paths, None) is None
    assert stress.checkout_problem(paths, str(paths.repo)) is None
    assert "WorkingDirectory" in str(stress.checkout_problem(paths, str(tmp_path / "elsewhere")))


def navidrome_songs_handler(missing: list[dict], songs, deleted: list[list[str]]):
    """Navidrome's native API: /api/missing, /api/song?path= (prefix, as LIKE 'p%')."""

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
        if request.method == "GET" and request.url.path == "/api/song":
            if songs is None:
                return httpx.Response(500)
            prefix = request.url.params["path"]
            return httpx.Response(200, json=[s for s in songs if s["path"].startswith(prefix)])
        return httpx.Response(404)

    return handler


def run_finish(paths, manifest, handler):
    stress.write_json(paths.manifest, manifest)
    nd = stress.Navidrome("http://nd", "admin", "pw")

    async def go():
        async with (
            client_for(lambda r: httpx.Response(200, json=[])) as client,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as nd_client,
        ):
            return await stress.finish_cleanup(
                client, nd_client, nd, paths, manifest, {"library_paths": []}, wait=0, step=0
            )

    errors, purged = asyncio.run(go())
    return errors, purged, json.loads(paths.manifest.read_text())


def deleted_probes(*result_paths):
    manifest = stress.new_manifest([PROBE_1, PROBE_2, PROBE_3][: len(result_paths)], None)
    for entry, result in zip(manifest["probes"], result_paths, strict=True):
        entry.update(result_path=result, deleted=True)
    return manifest


def test_a_probe_navidrome_never_scanned_does_not_block_the_cleanup(paths):
    known, never = "K/Singles/Known.m4a", "Kevin MacLeod/Singles/Strength of the Titans.m4a"
    manifest = deleted_probes(known, never)
    deleted: list[list[str]] = []
    songs = [
        {"path": known, "missing": True},
        # Тот же префикс, другой файл: LIKE 'путь%' находит и его.
        {"path": never + ".bak", "missing": False},
    ]
    handler = navidrome_songs_handler([{"id": "k1", "path": known}], songs, deleted)

    errors, purged, saved = run_finish(paths, manifest, handler)

    assert (errors, purged) == ([], [known])
    assert deleted == [["k1"]]
    assert saved["cleaned_at"]
    assert saved["probes"][0]["navidrome_purged"] is True
    assert saved["probes"][1]["navidrome_never_scanned"] is True
    assert "navidrome_purged" not in saved["probes"][1]


def test_a_probe_navidrome_still_plays_stays_an_error(paths):
    live = "L/Singles/Live.m4a"
    handler = navidrome_songs_handler([], [{"path": live, "missing": False}], [])
    errors, purged, saved = run_finish(paths, deleted_probes(live), handler)
    assert purged == []
    assert any(live in e for e in errors)
    assert saved["cleaned_at"] is None
    assert "navidrome_never_scanned" not in saved["probes"][0]


def test_a_failed_navidrome_lookup_stays_an_error(paths):
    lost = "Kevin MacLeod/Singles/Strength of the Titans.m4a"
    handler = navidrome_songs_handler([], None, [])
    errors, purged, saved = run_finish(paths, deleted_probes(lost), handler)
    assert any("lookup failed" in e for e in errors)
    assert not any("still knows" in e for e in errors), "a failed lookup proves nothing"
    assert saved["cleaned_at"] is None
    assert "navidrome_never_scanned" not in saved["probes"][0]
