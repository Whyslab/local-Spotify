"""The HTTP surface for playlists and the play journal."""

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, library, navidrome, playlists, runtime


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    # No credentials in a test run, so nothing reaches out over the network.
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True)
    library.invalidate_library_index()
    monkeypatch.setattr(library, "library_index", lambda: [])

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_playlists_require_auth(client):
    client.headers.pop("Authorization")
    assert client.get("/api/playlists").status_code == 401


def test_create_read_and_reorder(client):
    created = client.post("/api/playlists", json={"name": "Ночь", "paths": ["A.m4a", "B.m4a"]})
    assert created.status_code == 200
    assert [e["path"] for e in created.json()["entries"]] == ["A.m4a", "B.m4a"]

    read = client.get("/api/playlists/Ночь/tracks").json()
    reordered = client.put(
        "/api/playlists/Ночь/tracks",
        json={"paths": ["B.m4a", "A.m4a"], "revision": read["revision"]},
    )

    assert reordered.status_code == 200
    assert [e["path"] for e in reordered.json()["entries"]] == ["B.m4a", "A.m4a"]
    assert reordered.json()["revision"] != read["revision"]


def test_reorder_against_a_stale_revision_is_refused(client):
    client.post("/api/playlists", json={"name": "shared", "paths": ["A.m4a", "B.m4a"]})
    stale = client.get("/api/playlists/shared/tracks").json()["revision"]

    client.put("/api/playlists/shared/tracks", json={"paths": ["B.m4a"], "revision": stale})
    second = client.put(
        "/api/playlists/shared/tracks", json={"paths": ["A.m4a"], "revision": stale}
    )

    assert second.status_code == 409


def test_creating_the_same_playlist_twice_is_refused(client):
    client.post("/api/playlists", json={"name": "once", "paths": []})
    assert client.post("/api/playlists", json={"name": "once", "paths": []}).status_code == 409


def test_reading_a_missing_playlist_is_404(client):
    assert client.get("/api/playlists/nope/tracks").status_code == 404


def test_rename_and_delete(client):
    client.post("/api/playlists", json={"name": "old", "paths": ["A.m4a"]})

    renamed = client.patch("/api/playlists/old", json={"name": "new"})
    assert renamed.status_code == 200
    assert client.get("/api/playlists/old/tracks").status_code == 404

    deleted = client.delete("/api/playlists/new")
    assert deleted.status_code == 200
    assert client.get("/api/playlists/new/tracks").status_code == 404


def test_health_reports_playlists_and_journal(client):
    client.post("/api/playlists", json={"name": "counted", "paths": ["A.m4a"]})
    payload = client.get("/health").json()

    assert payload["playlists"] == 1
    assert payload["plays_logged"] == 0
    assert payload["navidrome_pending"] == 0


def test_play_journal_records_and_summarises(client):
    client.post(
        "/api/plays",
        json={"path": "A.m4a", "played_seconds": 180.0, "duration": 180.0, "skipped": False},
    )
    client.post(
        "/api/plays",
        json={"path": "B.m4a", "played_seconds": 4.0, "duration": 200.0, "skipped": True},
    )

    stats = client.get("/api/plays/stats").json()
    assert stats["plays"] == 2
    assert stats["skipped"] == 1
    assert stats["skip_rate"] == 0.5
    assert stats["distinct_tracks"] == 2


def test_play_journal_is_rate_limited(client, monkeypatch):
    """The endpoint is open on the LAN and a played track cannot arrive 60x a minute."""
    from adder import app as app_module

    monkeypatch.setattr(app_module, "PLAYS_PER_MINUTE", 2)
    app_module._PLAYS_WINDOW.clear()

    body = {"path": "A.m4a", "played_seconds": 1.0}
    assert client.post("/api/plays", json=body).status_code == 200
    assert client.post("/api/plays", json=body).status_code == 200
    assert client.post("/api/plays", json=body).status_code == 429
