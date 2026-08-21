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

    import importlib
    import app as app_module

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
