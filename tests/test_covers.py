"""Playlist covers: what counts as an image, and where the original lives."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from adder import config, covers, library, navidrome, playlists, runtime

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
WEBP = b"RIFF" + (40).to_bytes(4, "little") + b"WEBP" + b"VP8 " + b"\x00" * 32


@pytest.fixture()
def temp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    config.LIBRARY.mkdir(parents=True)
    library.invalidate_library_index()
    monkeypatch.setattr(library, "library_index", lambda: [])
    return tmp_path


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_type_comes_from_the_bytes(temp_env, payload, expected):
    assert covers.store("mix", payload) == expected
    assert covers.media_type("mix") == expected


def test_a_file_that_is_not_an_image_is_refused(temp_env):
    """The name and the client's Content-Type are both supplied by the caller."""
    with pytest.raises(HTTPException) as excinfo:
        covers.store("mix", b"#!/bin/sh\nrm -rf /\n")
    assert excinfo.value.status_code == 415


def test_an_oversized_cover_is_refused(temp_env, monkeypatch):
    monkeypatch.setattr(config, "MAX_COVER_BYTES", 64)
    with pytest.raises(HTTPException) as excinfo:
        covers.store("mix", PNG + b"\x00" * 200)
    assert excinfo.value.status_code == 413


def test_storing_again_replaces_the_previous_cover(temp_env):
    covers.store("mix", PNG)
    covers.store("mix", JPEG)

    files = sorted(covers.COVERS_DIR.glob("mix.*"))
    assert len(files) == 1
    assert files[0].suffix == ".jpg"


def test_rename_carries_the_cover_across(temp_env):
    """Navidrome loses the cover on a rename; the local copy is what restores it."""
    covers.store("old", PNG)
    covers.rename("old", "new")

    assert covers.cover_file("old") is None
    assert covers.read_cover("new")[0] == PNG


def test_read_cover_of_an_unknown_playlist_is_none(temp_env):
    assert covers.read_cover("never-had-one") is None


@pytest.fixture()
def client(temp_env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(navidrome, "configured", lambda: False)

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_upload_and_fetch_a_cover(client):
    client.post("/api/playlists", json={"name": "Ночь", "paths": []})

    uploaded = client.post(
        "/api/playlists/Ночь/cover",
        files={"image": ("photo.png", PNG, "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["media_type"] == "image/png"

    fetched = client.get("/api/playlists/Ночь/cover")
    assert fetched.status_code == 200
    assert fetched.content == PNG
    assert fetched.headers["content-type"] == "image/png"


def test_upload_to_a_missing_playlist_is_404(client):
    response = client.post(
        "/api/playlists/nope/cover", files={"image": ("photo.png", PNG, "image/png")}
    )
    assert response.status_code == 404


def test_a_disguised_file_is_refused(client):
    """A .png name and an image/png header on something that is not an image."""
    client.post("/api/playlists", json={"name": "mix", "paths": []})
    response = client.post(
        "/api/playlists/mix/cover",
        files={"image": ("innocent.png", b"not an image at all", "image/png")},
    )
    assert response.status_code == 415


def test_missing_cover_is_404(client):
    client.post("/api/playlists", json={"name": "bare", "paths": []})
    assert client.get("/api/playlists/bare/cover").status_code == 404


def test_cover_survives_a_playlist_rewrite(client):
    """Reordering rewrites the .m3u; the cover must not be collateral damage."""
    client.post("/api/playlists", json={"name": "mix", "paths": ["A.m4a", "B.m4a"]})
    client.post("/api/playlists/mix/cover", files={"image": ("c.png", PNG, "image/png")})

    revision = client.get("/api/playlists/mix/tracks").json()["revision"]
    client.put(
        "/api/playlists/mix/tracks", json={"paths": ["B.m4a", "A.m4a"], "revision": revision}
    )

    assert client.get("/api/playlists/mix/cover").content == PNG
    assert client.get("/api/playlists/mix/tracks").json()["cover"] is True


def test_deleting_a_playlist_removes_its_cover(client):
    client.post("/api/playlists", json={"name": "gone", "paths": []})
    client.post("/api/playlists/gone/cover", files={"image": ("c.png", PNG, "image/png")})

    client.delete("/api/playlists/gone")

    assert covers.cover_file("gone") is None
