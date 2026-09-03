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


def test_static_app_js_does_not_render_api_data_with_innerhtml(client):
    # The previous version of this test checked GET / (index.html), but
    # index.html only contains a <script src="/static/app.js"> tag - the
    # code that actually renders task.title/task.artist/task.url (all
    # derived from attacker-controlled YouTube metadata) lives in the
    # separately served app.js and was never inspected here. That let a
    # real stored-XSS regression (innerHTML template interpolation of
    # task fields, capable of exfiltrating the API token from
    # localStorage) ship silently. Check the file that matters.
    response = client.get("/static/app.js")

    assert response.status_code == 200

    js = response.text

    # Task fields must be inserted through safe DOM APIs such as
    # textContent, never by assigning untrusted values to innerHTML.
    # (Checks for an actual assignment, not just the word - the file's
    # own comments legitimately mention innerHTML when explaining why
    # it's avoided.)
    assert re.search(r"\.innerHTML\s*=", js) is None
    assert "textContent" in js
    assert "replaceChildren" in js


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
