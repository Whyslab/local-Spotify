import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adder import config, db, ingest, runtime
from adder import queue as adder_queue


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    """
    Import app with an isolated temporary SQLite database.
    No real YouTube downloads are performed by these tests.
    """
    monkeypatch.setenv("API_TOKEN", "test-secret")

    import sys

    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import importlib

    app_module = importlib.import_module("adder.app")

    # Isolate authentication from the real production token in adder/.env.
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")

    # Disable background workers for API unit tests.
    # Worker execution is covered by dedicated worker tests below.
    monkeypatch.setattr(config, "MAX_WORKERS", 0)

    # Isolate database from the real project database.
    monkeypatch.setattr(
        runtime,
        "DB_PATH",
        tmp_path / "test.db",
    )

    # Isolate temporary files.
    monkeypatch.setattr(
        runtime,
        "TMP_DIR",
        tmp_path / "tmp",
    )

    # Isolate the music library. Without this, /health's "is the library
    # folder present" check depends on whether ~/Music/Normalized Library
    # already exists on whatever machine runs the tests - true on a
    # machine with a real library, false on a clean checkout or CI runner
    # (see ci.yml, which points LIBRARY_PATH at a directory that is never
    # created). That made this fixture non-hermetic.
    monkeypatch.setattr(
        config,
        "LIBRARY",
        tmp_path / "library",
    )

    runtime.PROJECT.mkdir(parents=True, exist_ok=True)
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)

    return app_module


@pytest.fixture()
def client(app_module):
    with TestClient(app_module.app) as client:
        yield client


def auth_headers():
    return {"Authorization": "Bearer test-secret"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_tasks_requires_auth(client):
    response = client.get("/api/tasks")

    assert response.status_code == 401


def test_tasks_rejects_wrong_token(client):
    response = client.get(
        "/api/tasks",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_tasks_accepts_correct_token(client):
    response = client.get(
        "/api/tasks",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == []


def test_add_requires_auth(client):
    response = client.post(
        "/api/add",
        json={"links": ["https://www.youtube.com/watch?v=test-auth"]},
    )

    assert response.status_code == 401


def test_add_rejects_wrong_token(client):
    response = client.post(
        "/api/add",
        json={"links": ["https://www.youtube.com/watch?v=test-auth"]},
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_add_accepts_valid_youtube_url(client):
    response = client.post(
        "/api/add",
        json={"links": ["https://www.youtube.com/watch?v=test-valid"]},
        headers=auth_headers(),
    )

    assert response.status_code == 200

    body = response.json()

    assert "added" in body
    assert len(body["added"]) == 1
    assert isinstance(body["added"][0], int)


def test_tasks_returns_added_task(client):
    add_response = client.post(
        "/api/add",
        json={"links": ["https://www.youtube.com/watch?v=test-task"]},
        headers=auth_headers(),
    )

    assert add_response.status_code == 200

    response = client.get(
        "/api/tasks",
        headers=auth_headers(),
    )

    assert response.status_code == 200

    tasks = response.json()

    assert len(tasks) == 1
    assert tasks[0]["url"] == "https://www.youtube.com/watch?v=test-task"
    assert tasks[0]["status"] == "queued"


def test_duplicate_url_is_not_added_twice(client):
    url = "https://www.youtube.com/watch?v=test-duplicate"

    first = client.post(
        "/api/add",
        json={"links": [url]},
        headers=auth_headers(),
    )

    second = client.post(
        "/api/add",
        json={"links": [url]},
        headers=auth_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200

    assert len(first.json()["added"]) == 1
    assert second.json()["added"] == []


def test_invalid_url_is_rejected(client):
    response = client.post(
        "/api/add",
        json={"links": ["https://example.com/not-youtube"]},
        headers=auth_headers(),
    )

    assert response.status_code == 400


def test_health_does_not_require_auth(client):
    response = client.get("/health")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=xyz456",
        "https://m.youtube.com/watch?v=qwe789",
        "https://music.youtube.com/watch?v=asd987",
        "https://youtu.be/zxc654",
    ],
)
def test_supported_youtube_urls_are_accepted(client, url):
    response = client.post(
        "/api/add",
        json={"links": [url]},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert len(response.json()["added"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/not-youtube",
        "https://vimeo.com/123456",
        "https://evil-youtube.com/watch?v=abc123",
        "https://youtube.com.evil.example/watch?v=abc123",
    ],
)
def test_non_youtube_urls_are_rejected(client, url):
    response = client.post(
        "/api/add",
        json={"links": [url]},
        headers=auth_headers(),
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Security: XSS regression
# ---------------------------------------------------------------------------


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Every script the page can load, found rather than listed: views.js (the whole
# «Обложка» look) was missing from a hand-written list and went unchecked.
WEB_SCRIPTS = sorted(
    p.relative_to(WEB_DIR).as_posix()
    for p in WEB_DIR.rglob("*.js")
    if not any(part.startswith(".") for part in p.relative_to(WEB_DIR).parts)
)
# The ones that put library data on the page must do it through the safe APIs.
ROW_BUILDERS = {"app.js", "player.js", "views.js", "design/core.js", "design/v1.js"}


def test_the_script_list_covers_the_page():
    assert {"app.js", "player.js", "views.js", "offline.js", "look.js", "sw.js"} <= set(WEB_SCRIPTS)


@pytest.mark.parametrize("script", WEB_SCRIPTS)
def test_static_scripts_do_not_render_api_data_with_innerhtml(client, script):
    # The previous version of this test checked GET / (index.html), but
    # index.html only contains a <script src="/static/app.js"> tag - the
    # code that actually renders task.title/task.artist/task.url (all
    # derived from attacker-controlled YouTube metadata) lives in the
    # separately served app.js and was never inspected here. That let a
    # real stored-XSS regression (innerHTML template interpolation of
    # task fields, capable of exfiltrating the API token from
    # localStorage) ship silently. Check the files that matter -- player.js
    # renders playlist names and track titles from the same untrusted
    # sources, so it is under the same rule.
    response = client.get(f"/static/{script}")

    assert response.status_code == 200

    js = response.text

    # Task fields must be inserted through safe DOM APIs such as
    # textContent, never by assigning untrusted values to innerHTML.
    # (Checks for an actual assignment, not just the word - the file's
    # own comments legitimately mention innerHTML when explaining why
    # it's avoided.)
    assert re.search(r"\.innerHTML\s*=", js) is None
    # The other ways a string becomes markup. DOMParser is allowed only for
    # the icon set, which is parsed as SVG from constants in the file.
    assert (
        re.search(r"\.innerHTML\s*\+=|\.outerHTML\s*=|insertAdjacentHTML|document\.write", js)
        is None
    )
    assert '"text/html"' not in js
    # The two that build rows out of library data must use the safe APIs;
    # offline.js and look.js render no such data, only the absence is checked.
    if script in ROW_BUILDERS:
        assert "textContent" in js
        assert "replaceChildren" in js


def test_index_links_every_local_file_with_its_fingerprint(client):
    """Each script and stylesheet the player links carries ?v=<hash>: one
    left out of the list in index() is served stale from the browser cache,
    and new markup then meets an old style sheet (look.js, the fonts)."""
    html = client.get("/").text

    linked = re.findall(r'(?:src|href)="(/static/[^"?]+\.(?:js|css)(?:\?[^"]*)?)"', html)
    assert {"/static/look.js", "/static/fonts/fonts.css"} <= {h.split("?")[0] for h in linked}
    assert all("?v=" in href for href in linked)


def test_processing_url_remains_locked_during_retry(app_module, monkeypatch):
    url = "https://www.youtube.com/watch?v=retry-lock-test"
    task_id = 1

    runtime.PROCESSING_URLS.clear()
    runtime.shutdown_event.clear()

    calls = []

    def fake_yt_meta(_url):
        calls.append("attempt")
        if len(calls) == 1:
            raise RuntimeError("network timeout")
        return {
            "id": "retry-lock-video",
            "title": "Test Track",
            "artist": "Test Artist",
        }

    def fake_task_update(*args, **kwargs):
        pass

    def fake_yt_download(*args, **kwargs):
        raise RuntimeError("stop after retry")

    monkeypatch.setattr(ingest, "yt_meta", fake_yt_meta)
    monkeypatch.setattr(db, "task_update", fake_task_update)
    monkeypatch.setattr(ingest, "yt_download", fake_yt_download)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    monkeypatch.setattr(config, "RETRY_BACKOFF_BASE", 1)

    original_wait = runtime.shutdown_event.wait

    def check_lock_during_backoff(timeout):
        assert url in runtime.PROCESSING_URLS
        return original_wait(0)

    monkeypatch.setattr(runtime.shutdown_event, "wait", check_lock_during_backoff)

    runtime.PROCESSING_URLS.add(url)

    adder_queue.process(task_id, url)

    assert len(calls) == 2
    assert url not in runtime.PROCESSING_URLS


def test_failed_url_can_be_requeued_without_duplicate(client, app_module):
    url = "https://www.youtube.com/watch?v=failed-retry-test"

    # Create the original task through the API.
    first = client.post(
        "/api/add",
        json={"links": [url]},
        headers=auth_headers(),
    )

    assert first.status_code == 200
    first_id = first.json()["added"][0]

    # Simulate a permanently failed task.
    db.task_update(
        first_id,
        status="error",
        artist="Old Artist",
        title="Old Title",
        error="download failed",
        error_type="network",
        retry_count=3,
    )

    # Remove the simulated task from the in-memory processing lock so the
    # API follows the database retry path.
    with runtime.FILE_LOCK:
        runtime.PROCESSING_URLS.discard(url)

    # Re-submit the same URL.
    second = client.post(
        "/api/add",
        json={"links": [url]},
        headers=auth_headers(),
    )

    assert second.status_code == 200

    body = second.json()
    assert body["added"] == [first_id]

    # Verify that the same database row was reused.
    tasks = client.get(
        "/api/tasks",
        headers=auth_headers(),
    )

    assert tasks.status_code == 200

    matching = [task for task in tasks.json() if task["url"] == url]

    assert len(matching) == 1

    task = matching[0]

    assert task["id"] == first_id
    assert task["status"] == "queued"
    assert task["artist"] is None
    assert task["title"] is None
    assert task["error"] is None
    assert task["error_type"] is None
    assert task["retry_count"] == 0


def test_shutdown_during_retry_releases_processing_lock(app_module, monkeypatch):
    url = "https://www.youtube.com/watch?v=shutdown-cleanup-test"
    task_id = 1

    runtime.PROCESSING_URLS.clear()
    runtime.shutdown_event.clear()

    attempts = []

    def fake_yt_meta(_url):
        attempts.append("attempt")
        raise RuntimeError("network timeout")

    def fake_task_update(*args, **kwargs):
        pass

    monkeypatch.setattr(ingest, "yt_meta", fake_yt_meta)
    monkeypatch.setattr(db, "task_update", fake_task_update)
    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    monkeypatch.setattr(config, "RETRY_BACKOFF_BASE", 1)

    runtime.PROCESSING_URLS.add(url)

    def trigger_shutdown(timeout):
        runtime.shutdown_event.set()
        return True

    monkeypatch.setattr(
        runtime.shutdown_event,
        "wait",
        trigger_shutdown,
    )

    adder_queue.process(task_id, url)

    assert attempts == ["attempt"]
    assert url not in runtime.PROCESSING_URLS


# ---------------------------------------------------------------------------
# Worker / startup lifecycle
# ---------------------------------------------------------------------------


def test_recover_queued_tasks_requeues_interrupted_tasks(app_module):
    db.db_init()

    queued_id = db.db_exec(
        """
        INSERT INTO tasks(url, status)
        VALUES (?, ?)
        """,
        ("https://www.youtube.com/watch?v=queued-recovery", "queued"),
    ).lastrowid

    downloading_id = db.db_exec(
        """
        INSERT INTO tasks(url, status)
        VALUES (?, ?)
        """,
        ("https://www.youtube.com/watch?v=downloading-recovery", "downloading"),
    ).lastrowid

    tagging_id = db.db_exec(
        """
        INSERT INTO tasks(url, status)
        VALUES (?, ?)
        """,
        ("https://www.youtube.com/watch?v=tagging-recovery", "tagging"),
    ).lastrowid

    runtime.PROCESSING_URLS.clear()

    while not runtime.TASK_QUEUE.empty():
        try:
            runtime.TASK_QUEUE.get_nowait()
            runtime.TASK_QUEUE.task_done()
        except Exception:
            break

    adder_queue.recover_queued_tasks()

    tasks = db.db_query("SELECT id, url, status FROM tasks ORDER BY id")

    assert len(tasks) == 3
    assert tasks[0]["id"] == queued_id
    assert tasks[0]["status"] == "queued"
    assert tasks[1]["id"] == downloading_id
    assert tasks[1]["status"] == "queued"
    assert tasks[2]["id"] == tagging_id
    assert tasks[2]["status"] == "queued"

    recovered = []

    while True:
        try:
            item = runtime.TASK_QUEUE.get_nowait()
        except Exception:
            break

        recovered.append(item)
        runtime.TASK_QUEUE.task_done()

    assert len(recovered) == 3
    assert {item[0] for item in recovered} == {
        queued_id,
        downloading_id,
        tagging_id,
    }

    assert {item[1] for item in recovered} == {
        "https://www.youtube.com/watch?v=queued-recovery",
        "https://www.youtube.com/watch?v=downloading-recovery",
        "https://www.youtube.com/watch?v=tagging-recovery",
    }


def test_recover_queued_tasks_does_not_duplicate_processing_urls(app_module):
    db.db_init()

    url = "https://www.youtube.com/watch?v=recovery-duplicate"

    task_id = db.db_exec(
        """
        INSERT INTO tasks(url, status)
        VALUES (?, ?)
        """,
        (url, "queued"),
    ).lastrowid

    runtime.PROCESSING_URLS.clear()

    while not runtime.TASK_QUEUE.empty():
        try:
            runtime.TASK_QUEUE.get_nowait()
            runtime.TASK_QUEUE.task_done()
        except Exception:
            break

    runtime.PROCESSING_URLS.add(url)

    adder_queue.recover_queued_tasks()

    assert runtime.TASK_QUEUE.empty()
    assert url in runtime.PROCESSING_URLS
    assert task_id > 0


def test_cleanup_old_temp_files_removes_only_expired_files(app_module):
    import time

    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)

    old_file = runtime.TMP_DIR / "old_processing.m4a"
    fresh_file = runtime.TMP_DIR / "fresh_processing.m4a"

    old_file.write_bytes(b"old")
    fresh_file.write_bytes(b"fresh")

    now = time.time()
    old_timestamp = now - (runtime.TMP_TTL_SECONDS + 60)

    os.utime(old_file, (old_timestamp, old_timestamp))

    ingest.cleanup_old_temp_files()

    assert not old_file.exists()
    assert fresh_file.exists()


def test_worker_processes_queue_and_calls_task_done(app_module, monkeypatch):
    import threading

    task_id = 123
    url = "https://www.youtube.com/watch?v=worker-test"

    processed = []

    def fake_process(tid, task_url):
        processed.append((tid, task_url))
        runtime.shutdown_event.set()

    monkeypatch.setattr(adder_queue, "process", fake_process)

    runtime.shutdown_event.clear()

    while not runtime.TASK_QUEUE.empty():
        try:
            runtime.TASK_QUEUE.get_nowait()
            runtime.TASK_QUEUE.task_done()
        except Exception:
            break

    runtime.TASK_QUEUE.put((task_id, url))

    worker_thread = threading.Thread(
        target=adder_queue.worker,
        daemon=True,
    )
    worker_thread.start()
    worker_thread.join(timeout=2)

    assert not worker_thread.is_alive()
    assert processed == [(task_id, url)]

    runtime.TASK_QUEUE.join()


def test_worker_stops_without_processing_when_shutdown_is_set(
    app_module,
    monkeypatch,
):
    processed = []

    def fake_process(*args):
        processed.append(args)

    monkeypatch.setattr(adder_queue, "process", fake_process)

    runtime.shutdown_event.set()

    runtime.TASK_QUEUE.put(
        (
            999,
            "https://www.youtube.com/watch?v=should-not-run",
        )
    )

    adder_queue.worker()

    assert processed == []

    runtime.TASK_QUEUE.task_done()
    runtime.shutdown_event.clear()


# ---------------------------------------------------------------------------
# YouTube URL canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc12345678",
        "https://youtube.com/watch?v=abc12345678",
        "https://m.youtube.com/watch?v=abc12345678",
        "https://music.youtube.com/watch?v=abc12345678",
        "https://youtu.be/abc12345678",
        "https://www.youtu.be/abc12345678",
    ],
)
def test_youtube_urls_are_canonicalized(app_module, url):
    assert ingest.canonicalize_youtube_url(url) == ("https://www.youtube.com/watch?v=abc12345678")


def test_youtube_canonicalization_rejects_invalid_video_id(app_module):
    with pytest.raises(ValueError):
        ingest.canonicalize_youtube_url("https://www.youtube.com/watch?v=invalid%20video%21")


def test_equivalent_youtube_urls_are_not_added_twice(
    app_module,
    monkeypatch,
):
    db.db_init()
    runtime.PROCESSING_URLS.clear()

    while not runtime.TASK_QUEUE.empty():
        try:
            runtime.TASK_QUEUE.get_nowait()
            runtime.TASK_QUEUE.task_done()
        except Exception:
            break

    youtube_id = "CCHdMIEGaaM"

    first = app_module.add(
        app_module.AddRequest(links=[f"https://youtu.be/{youtube_id}"]),
        authenticated=True,
    )

    second = app_module.add(
        app_module.AddRequest(links=[f"https://www.youtube.com/watch?v={youtube_id}"]),
        authenticated=True,
    )

    assert len(first["added"]) == 1
    assert second["added"] == []

    rows = db.db_query(
        "SELECT url FROM tasks WHERE url = ?",
        (f"https://www.youtube.com/watch?v={youtube_id}",),
    )

    assert len(rows) == 1


def test_health_is_unhealthy_without_ffmpeg(client, monkeypatch):
    monkeypatch.setattr(
        ingest, "check_dependencies", lambda: {"ffmpeg": "missing", "js_runtime": "ok"}
    )

    response = client.get("/health", headers=auth_headers())

    assert response.status_code == 503
    assert response.json()["ffmpeg"] == "missing"


def test_health_stays_healthy_without_deno(client, monkeypatch):
    monkeypatch.setattr(
        ingest, "check_dependencies", lambda: {"ffmpeg": "ok", "js_runtime": "missing"}
    )

    response = client.get("/health", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["js_runtime"] == "missing"


def test_final_failure_is_logged_with_its_type(app_module, monkeypatch, caplog):
    def fake_yt_meta(_url):
        raise RuntimeError("ERROR: [youtube] abc: Video unavailable")

    monkeypatch.setattr(ingest, "yt_meta", fake_yt_meta)
    monkeypatch.setattr(db, "task_update", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "check_disk_space", lambda *a, **k: (True, 10_000))

    with caplog.at_level("INFO"):
        adder_queue.process(7, "https://www.youtube.com/watch?v=abc")

    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(failures) == 1
    assert failures[0].task_id == 7
    assert "youtube_not_found" in failures[0].getMessage()


# ---------------------------------------------------------------------------
# Retrying failed tasks: by hand, and after a rate limit by itself
# ---------------------------------------------------------------------------


def _failed(url, error_type="network_error", minutes_ago=0, **extra):
    tid = db.db_exec(
        "INSERT INTO tasks(url, status, error_type, updated_at) "
        "VALUES(?, 'error', ?, datetime('now', 'localtime', ?))",
        (url, error_type, f"-{minutes_ago} minutes"),
    ).lastrowid
    if extra:
        db.task_update(tid, **extra)
        db.db_exec(
            "UPDATE tasks SET updated_at = datetime('now', 'localtime', ?) WHERE id = ?",
            (f"-{minutes_ago} minutes", tid),
        )
    return tid


def _drain():
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
        runtime.TASK_QUEUE.task_done()


def test_retry_failed_queues_every_failed_link(client, app_module):
    _drain()
    runtime.PROCESSING_URLS.clear()
    a = _failed("https://www.youtube.com/watch?v=a")
    b = _failed("https://www.youtube.com/watch?v=b", "youtube_not_found")
    db.db_exec("INSERT INTO tasks(url, status) VALUES('https://www.youtube.com/watch?v=c', 'done')")

    response = client.post("/api/tasks/retry-failed", headers=auth_headers())

    assert response.status_code == 200
    assert sorted(response.json()["requeued"]) == sorted([a, b])
    statuses = {r["id"]: r["status"] for r in db.db_query("SELECT id, status FROM tasks")}
    assert statuses[a] == statuses[b] == "queued"
    _drain()


def test_retry_failed_needs_the_token(client):
    assert client.post("/api/tasks/retry-failed").status_code == 401


def test_a_failed_replacement_and_a_vanished_upload_are_left_alone(client, app_module):
    _drain()
    runtime.PROCESSING_URLS.clear()
    _failed("https://www.youtube.com/watch?v=r", replace_of="A/Singles/B.m4a")
    _failed("file:" + "0" * 64)

    assert app_module.requeue_failed() == []


def test_rate_limited_tasks_come_back_by_themselves_after_an_hour(client, app_module):
    _drain()
    runtime.PROCESSING_URLS.clear()
    old = _failed("https://www.youtube.com/watch?v=old", "rate_limited", minutes_ago=61)
    _failed("https://www.youtube.com/watch?v=new", "rate_limited", minutes_ago=5)
    _failed("https://www.youtube.com/watch?v=gone", "youtube_not_found", minutes_ago=120)
    tired = _failed(
        "https://www.youtube.com/watch?v=tired", "rate_limited", minutes_ago=90, auto_requeues=3
    )

    assert app_module.requeue_failed(auto=True) == [old]
    row = db.db_query("SELECT status, auto_requeues FROM tasks WHERE id = ?", (old,))[0]
    assert row == {"status": "queued", "auto_requeues": 1}
    assert db.db_query("SELECT status FROM tasks WHERE id = ?", (tired,))[0]["status"] == "error"
    _drain()


def test_a_manual_retry_resets_the_automatic_count(client, app_module):
    _drain()
    runtime.PROCESSING_URLS.clear()
    tid = _failed("https://www.youtube.com/watch?v=x", "rate_limited", 90, auto_requeues=3)

    assert app_module.requeue_failed() == [tid]
    assert db.db_query("SELECT auto_requeues FROM tasks WHERE id = ?", (tid,))[0] == {
        "auto_requeues": 0
    }
    _drain()


def test_the_panel_offers_retry_only_for_failures():
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text()

    assert 'retry.hidden = !queue.some(t => t.status === "error")' in js


def test_the_iphone_shortcut_request_is_accepted(client, app_module):
    """The exact request docs/iphone-shortcut.md builds: JSON {"links": [url]}.

    A link shared from the YouTube app carries a ?si= tracking parameter; it
    must land as the same canonical task as the plain link.
    """
    _drain()
    runtime.PROCESSING_URLS.clear()

    first = client.post(
        "/api/add",
        json={"links": ["https://youtu.be/dQw4w9WgXcQ?si=AbCdEf123"]},
        headers={**auth_headers(), "Content-Type": "application/json"},
    )
    again = client.post(
        "/api/add",
        json={"links": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]},
        headers=auth_headers(),
    )

    assert first.status_code == 200 and len(first.json()["added"]) == 1
    # The shortcut reads an empty "added" as "already there".
    assert again.json()["added"] == []
    _drain()


def _csp_directives(header: str) -> dict[str, list[str]]:
    out = {}
    for part in header.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def test_index_sends_csp(client):
    """A second line behind textContent: no foreign scripts, frames or form targets.

    'unsafe-inline' stays in script-src for the onclick= handlers of index.html,
    so it does not stop a handler injected into the page; textContent and the
    innerHTML guard above are the first line, this is the second.
    """
    response = client.get("/")
    csp = _csp_directives(response.headers["content-security-policy"])
    assert csp["default-src"] == ["'self'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["connect-src"] == ["'self'"]
    assert "https:" not in csp["script-src"] and "*" not in csp["script-src"]
    # Deezer's CDN: album search, artist photos and releases set it straight.
    assert "https://*.dzcdn.net" in csp["img-src"]


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        # The shapes Deezer's API hands out in cover_medium / picture_medium.
        ("https://e-cdns-images.dzcdn.net/images/cover/1a2b/250x250-000000-80-0-0.jpg", True),
        ("https://cdn-images.dzcdn.net/images/artist/9f/250x250-000000-80-0-0.jpg", True),
        ("https://cdns-images.dzcdn.net/images/cover/x/250x250.jpg", True),
        ("http://cdn-images.dzcdn.net/a.jpg", False),
        ("https://dzcdn.net.evil.com/a.jpg", False),
        ("https://evil.com/a.jpg", False),
    ],
)
def test_backend_image_hosts_within_csp(client, url, allowed):
    from urllib.parse import urlsplit

    sources = _csp_directives(client.get("/").headers["content-security-policy"])["img-src"]
    parts = urlsplit(url)

    def matches(source: str) -> bool:
        if not source.startswith("https://"):
            return False
        host = source[len("https://") :]
        if host.startswith("*."):
            return parts.scheme == "https" and parts.hostname.endswith(host[1:])
        return parts.scheme == "https" and parts.hostname == host

    assert any(matches(s) for s in sources) is allowed


def test_play_keeps_time_it_was_heard(client, app_module, monkeypatch):
    """Телефон копит прослушивания без сети и шлёт их позже. Журнал и
    ListenBrainz должны получить время, когда трек звучал, а не когда дошёл
    запрос: иначе «давно не звучало» и история врут на часы и дни."""
    import time
    from datetime import datetime

    from adder import library, listenbrainz

    monkeypatch.setattr(app_module, "_PLAYS_WINDOW", {})
    monkeypatch.setattr(config, "PLAY_HISTORY_DAYS", 30)
    monkeypatch.setattr(config, "LISTENBRAINZ_TOKEN", "lb-token")
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [{"path": "A/Singles/B.m4a", "artist": "A", "title": "B", "album": ""}],
    )

    def local(epoch: float) -> str:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M:%S")

    def play(**extra):
        body = {"path": "A/Singles/B.m4a", "played_seconds": 200, "duration": 248, **extra}
        return client.post("/api/plays", json=body, headers=auth_headers())

    def last():
        row = db.db_query("SELECT played_at FROM plays ORDER BY id DESC LIMIT 1")[0]
        listen = db.db_query("SELECT listened_at FROM listens ORDER BY id DESC LIMIT 1")[0]
        return row["played_at"], listen["listened_at"]

    # Heard three days ago (listening started then), sent now.
    heard = time.time() - 3 * 86400
    assert play(heard_at=heard).status_code == 200
    played_at, listened_at = last()
    assert listened_at == int(heard)  # ListenBrainz: when listening started
    assert played_at == local(heard + 200)  # journal: when the play ended, as before

    # Too far in the future (a broken clock) and older than the journal keeps.
    count = len(db.db_query("SELECT id FROM plays"))
    assert play(heard_at=time.time() + 3600).status_code == 422
    assert play(heard_at=time.time() - 31 * 86400).status_code == 422
    assert play(heard_at="yesterday").status_code == 422
    assert len(db.db_query("SELECT id FROM plays")) == count

    # A clock slightly ahead is accepted, but nothing lands in the future.
    before = time.time()
    assert play(heard_at=before + 120).status_code == 200
    played_at, listened_at = last()
    assert played_at <= local(time.time())
    assert listened_at <= int(time.time()) - 200 + 1

    # An old queued play without the field still counts, as of now.
    before = time.time()
    assert play().status_code == 200
    played_at, listened_at = last()
    assert local(before) <= played_at <= local(time.time())
    assert int(before) - 200 <= listened_at <= int(time.time()) - 200 + 1
    assert listenbrainz.pending_count() == 3
