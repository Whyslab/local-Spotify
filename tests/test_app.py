import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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

    # Isolate database from the real project database.
    monkeypatch.setattr(
        app_module,
        "DB_PATH",
        tmp_path / "test.db",
    )

    # Isolate temporary files.
    monkeypatch.setattr(
        app_module,
        "TMP_DIR",
        tmp_path / "tmp",
    )

    app_module.PROJECT.mkdir(parents=True, exist_ok=True)
    app_module.TMP_DIR.mkdir(parents=True, exist_ok=True)

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
        json={
            "links": [
                "https://www.youtube.com/watch?v=test-auth"
            ]
        },
    )

    assert response.status_code == 401


def test_add_rejects_wrong_token(client):
    response = client.post(
        "/api/add",
        json={
            "links": [
                "https://www.youtube.com/watch?v=test-auth"
            ]
        },
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_add_accepts_valid_youtube_url(client):
    response = client.post(
        "/api/add",
        json={
            "links": [
                "https://www.youtube.com/watch?v=test-valid"
            ]
        },
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
        json={
            "links": [
                "https://www.youtube.com/watch?v=test-task"
            ]
        },
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
        json={
            "links": [
                "https://example.com/not-youtube"
            ]
        },
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
        "https://youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://music.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
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

def test_index_does_not_render_api_data_with_innerhtml(client):
    response = client.get("/")

    assert response.status_code == 200

    html = response.text

    # API task fields must be inserted through safe DOM APIs such as
    # textContent, never by assigning untrusted values to innerHTML.
    assert "document.getElementById('tb').innerHTML" not in html
    assert "tbody.innerHTML" not in html
    assert "textContent" in html
    assert "replaceChildren()" in html


def test_processing_url_remains_locked_during_retry(app_module, monkeypatch):
    url = "https://www.youtube.com/watch?v=retry-lock-test"
    task_id = 1

    app_module.PROCESSING_URLS.clear()
    app_module.shutdown_event.clear()

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

    monkeypatch.setattr(app_module, "yt_meta", fake_yt_meta)
    monkeypatch.setattr(app_module, "task_update", fake_task_update)
    monkeypatch.setattr(app_module, "yt_download", fake_yt_download)
    monkeypatch.setattr(app_module, "MAX_RETRIES", 2)
    monkeypatch.setattr(app_module, "RETRY_BACKOFF_BASE", 1)

    original_wait = app_module.shutdown_event.wait

    def check_lock_during_backoff(timeout):
        assert url in app_module.PROCESSING_URLS
        return original_wait(0)

    monkeypatch.setattr(app_module.shutdown_event, "wait", check_lock_during_backoff)

    app_module.PROCESSING_URLS.add(url)

    app_module.process(task_id, url)

    assert len(calls) == 2
    assert url not in app_module.PROCESSING_URLS


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
    app_module.task_update(
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
    with app_module.FILE_LOCK:
        app_module.PROCESSING_URLS.discard(url)

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

    matching = [
        task for task in tasks.json()
        if task["url"] == url
    ]

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

    app_module.PROCESSING_URLS.clear()
    app_module.shutdown_event.clear()

    attempts = []

    def fake_yt_meta(_url):
        attempts.append("attempt")
        raise RuntimeError("network timeout")

    def fake_task_update(*args, **kwargs):
        pass

    monkeypatch.setattr(app_module, "yt_meta", fake_yt_meta)
    monkeypatch.setattr(app_module, "task_update", fake_task_update)
    monkeypatch.setattr(app_module, "MAX_RETRIES", 3)
    monkeypatch.setattr(app_module, "RETRY_BACKOFF_BASE", 1)

    app_module.PROCESSING_URLS.add(url)

    def trigger_shutdown(timeout):
        app_module.shutdown_event.set()
        return True

    monkeypatch.setattr(
        app_module.shutdown_event,
        "wait",
        trigger_shutdown,
    )

    app_module.process(task_id, url)

    assert attempts == ["attempt"]
    assert url not in app_module.PROCESSING_URLS
