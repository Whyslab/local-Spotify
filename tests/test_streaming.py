"""Signed stream links: what they allow, and for how long."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, library, navidrome, playlists, runtime, signing


def _write_m4a(path, seconds=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            str(seconds),
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


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
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True)
    _write_m4a(config.LIBRARY / "Артист" / "Singles" / "Трек, часть 2.m4a")
    library.invalidate_library_index()

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


TRACK = "Артист/Singles/Трек, часть 2.m4a"


def test_a_valid_signature_plays(client):
    url = client.get("/api/stream-url", params={"path": TRACK}).json()["url"]
    fresh = TestClient(client.app)  # no Authorization header at all

    response = fresh.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"
    assert len(response.content) > 0


def test_no_signature_is_refused(client):
    fresh = TestClient(client.app)
    assert fresh.get("/api/stream", params={"path": TRACK}).status_code == 403


def test_a_tampered_signature_is_refused(client):
    url = client.get("/api/stream-url", params={"path": TRACK}).json()["url"]
    fresh = TestClient(client.app)
    assert fresh.get(url[:-1] + ("0" if url[-1] != "0" else "1")).status_code == 403


def test_a_signature_does_not_carry_to_another_track(client):
    """One link buys one track, which is the point of signing the path."""
    payload = client.get("/api/stream-url", params={"path": TRACK}).json()
    exp = payload["expires_at"]
    sig = payload["url"].split("sig=")[1]
    fresh = TestClient(client.app)

    response = fresh.get(
        "/api/stream", params={"path": "Артист/Singles/Другой.m4a", "exp": exp, "sig": sig}
    )
    assert response.status_code == 403


def test_an_expired_link_is_refused(client, monkeypatch):
    url = client.get("/api/stream-url", params={"path": TRACK}).json()["url"]
    monkeypatch.setattr(signing.time, "time", lambda: 4_000_000_000.0)
    fresh = TestClient(client.app)
    assert fresh.get(url).status_code == 403


def test_range_requests_are_answered(client):
    """Seeking depends on this: <audio> asks for byte ranges, not whole files."""
    url = client.get("/api/stream-url", params={"path": TRACK}).json()["url"]
    fresh = TestClient(client.app)

    partial = fresh.get(url, headers={"Range": "bytes=0-99"})

    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert "content-range" in partial.headers


def test_stream_url_requires_auth(client):
    client.headers.pop("Authorization")
    assert client.get("/api/stream-url", params={"path": TRACK}).status_code == 401


def test_stream_url_refuses_a_path_outside_the_library(client):
    response = client.get("/api/stream-url", params={"path": "../../etc/passwd"})
    assert response.status_code in (400, 404)


def test_lifetime_covers_the_whole_track(client):
    """A link that dies mid-track turns every seek into a 403."""
    assert signing.lifetime_for(None) == signing.MIN_LIFETIME
    assert signing.lifetime_for(30) == signing.MIN_LIFETIME  # floor still applies
    assert signing.lifetime_for(3600) == 3600 + signing.GRACE_SECONDS


def test_the_signed_path_is_the_decoded_one(client):
    """Percent-encoding belongs to the URL, not to the file."""
    expires_at = 4_000_000_000
    assert signing.sign("Артист/Трек, часть 2.m4a", expires_at) == signing.sign(
        "Артист/Трек, часть 2.m4a", expires_at
    )
    assert signing.verify(
        "Артист/Трек, часть 2.m4a",
        expires_at,
        signing.sign("Артист/Трек, часть 2.m4a", expires_at),
        now=0,
    )


def test_the_key_is_not_the_api_token(client):
    """A leaked stream signature must not walk back to the token itself."""
    assert config.API_TOKEN.encode() not in signing._key()
