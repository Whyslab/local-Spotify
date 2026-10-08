"""Signed stream links: what they allow, and for how long."""

import os
import re
import subprocess
from pathlib import Path

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


def test_a_correctly_signed_link_still_cannot_leave_the_library(client):
    """Defence in depth: /api/stream-url refuses such a path, but should a
    signature for one ever exist, /api/stream itself must not follow it."""
    import time as _time

    from adder import config

    # An audio file right outside the library: only the "inside the library"
    # check stands between it and the stream, not the suffix check.
    outside = config.LIBRARY.parent / "outside.m4a"
    outside.write_bytes((Path(__file__).parent / "fixtures" / "tone.m4a").read_bytes())
    fresh = TestClient(client.app)
    for path in ("../outside.m4a", "../../etc/passwd", "A/../../outside.m4a"):
        expires = int(_time.time()) + 600
        response = fresh.get(
            "/api/stream",
            params={"path": path, "exp": expires, "sig": signing.sign(path, expires)},
        )
        assert response.status_code in (400, 403, 404), path


# ---------------------------------------------------------------------------
# Compression and browser caching
# ---------------------------------------------------------------------------

GZIP = {"Accept-Encoding": "gzip"}
YEAR = "max-age=31536000"


def _linked(client):
    """Every script and stylesheet the page links, as it links them."""
    return re.findall(r'(?:src|href)="(/static/[^"]+)"', client.get("/").text)


def test_fingerprinted_static_is_immutable_and_compressed(client):
    """A ?v=<hash> address never changes content, so the browser keeps it a
    year without asking; the text is sent gzipped (the phone is on Wi-Fi, the
    scripts and the style sheet are most of the first load)."""
    linked = [url for url in _linked(client) if "?v=" in url]
    assert {u.split("?")[0] for u in linked} >= {"/static/app.js", "/static/style.css"}
    for url in linked:
        response = client.get(url, headers=GZIP)
        assert response.status_code == 200, url
        assert YEAR in response.headers["cache-control"], url
        assert "immutable" in response.headers["cache-control"], url
        assert response.headers.get("content-encoding") == "gzip", url


def test_index_fingerprints_cached_by_mtime(client, tmp_path, monkeypatch):
    """GET / read and hashed all seven linked files on every load (B22). A
    file is read again only when its mtime or size changed."""
    from adder.app import static_stamp

    reads = []
    real = Path.read_bytes

    def counting(self):
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    client.get("/")
    reads.clear()
    client.get("/")
    assert reads == []

    script = tmp_path / "app.js"
    script.write_text("one")
    first = static_stamp(script)
    script.write_text("two")  # same size: the mtime tells
    os.utime(script, ns=(script.stat().st_atime_ns, script.stat().st_mtime_ns + 1_000_000))
    assert static_stamp(script) != first


def test_unfingerprinted_static_is_not_immutable(client):
    """Without a fingerprint the address outlives its content: a year in the
    browser's cache would keep an old file after an update. Fonts are linked
    from fonts.css without one, and a stamp that is not the file's (a page
    from before the update) must not pin the new content either."""
    css = client.get("/static/fonts/fonts.css").text
    fonts = ["/static/fonts/" + name for name in re.findall(r"url\(([^)]+)\)", css)]
    assert fonts
    plain = [url for url in _linked(client) if "?v=" not in url]
    stale = [url.split("?")[0] + "?v=0000000000" for url in _linked(client) if "?v=" in url]
    for url in ["/", "/sw.js", "/static/app.js", *plain, *fonts, *stale]:
        response = client.get(url, headers=GZIP)
        assert response.status_code == 200, url
        assert "immutable" not in response.headers.get("cache-control", ""), url
        assert YEAR not in response.headers.get("cache-control", ""), url


def test_compressed_response_varies_on_accept_encoding(client):
    """A cache in between must not hand the gzipped body to a client that did
    not ask for it, and the two bodies are not the same entity."""
    for url in ["/", "/static/app.js", "/static/style.css"]:
        packed = client.get(url, headers=GZIP)
        plain = client.get(url, headers={"Accept-Encoding": "identity"})
        assert packed.headers.get("content-encoding") == "gzip", url
        assert "content-encoding" not in plain.headers, url
        for response in (packed, plain):
            assert "accept-encoding" in response.headers.get("vary", "").lower(), url
        assert packed.content == plain.content  # the client decodes it
        if "etag" in plain.headers:
            assert packed.headers["etag"] != plain.headers["etag"], url


def test_stream_is_never_compressed(client):
    """Audio is compressed already, and a gzipped body breaks Range: the
    player seeks by byte offsets of the file."""
    url = client.get("/api/stream-url", params={"path": TRACK}).json()["url"]
    fresh = TestClient(client.app)
    whole = fresh.get(url, headers=GZIP)
    part = fresh.get(url, headers={**GZIP, "Range": "bytes=0-99"})
    assert whole.status_code == 200 and part.status_code == 206
    for response in (whole, part):
        assert "content-encoding" not in response.headers
