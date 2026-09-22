"""Мелочи из полного разбора проекта 23.09.2026 — каждая когда-то была ошибкой.

Служба слушает сеть общежития, поэтому половина здесь — про то, что видно
без ключа и что происходит с кривым вводом.
"""

import pytest
from fastapi.testclient import TestClient

from adder import config, covers, enrich, ingest, library, navidrome, playlists, runtime, signing


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
    library.invalidate_library_index()

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client


AUTH = {"Authorization": "Bearer test-secret"}


def test_health_without_a_token_says_only_whether_it_is_alive(client):
    short = client.get("/health").json()
    assert short == {"status": "healthy"}
    full = client.get("/health", headers=AUTH).json()
    assert "library_path" in full and "active_tasks" in full


def test_the_api_map_is_not_published(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_a_non_ascii_token_is_refused_not_a_crash(client):
    response = client.get("/api/playlists", headers={"Authorization": "Bearer ключ".encode()})
    assert response.status_code == 401


def test_a_non_ascii_signature_is_refused_not_a_crash():
    assert signing.verify("a.m4a", 9999999999, "подпись") is False


@pytest.mark.parametrize("bad", ["a.m4a\nb.m4a", "#EXTINF:1,x", "   ", "a\rb"])
def test_a_path_that_would_break_the_m3u_is_refused(client, bad):
    playlists.create("p", ["a.m4a"])
    response = client.put("/api/playlists/p/tracks", headers=AUTH, json={"paths": ["a.m4a", bad]})
    assert response.status_code == 400
    assert [line.path for line in playlists.read("p").entries] == ["a.m4a"]


def test_rename_never_overwrites_an_existing_playlist(client):
    playlists.create("a", ["1.m4a"])
    playlists.create("b", ["2.m4a"])
    with pytest.raises(Exception):  # noqa: B017 — HTTPException 409
        playlists.rename("a", "b")
    assert [line.path for line in playlists.read("b").entries] == ["2.m4a"]
    assert [line.path for line in playlists.read("a").entries] == ["1.m4a"]


def test_a_playlist_file_not_in_utf8_does_not_break_the_list(client):
    playlists.create("ok", ["a.m4a"])
    (config.LIBRARY / "старая.m3u").write_bytes("#EXTM3U\nПесня.m4a\n".encode("cp1251"))
    names = [row["name"] for row in client.get("/api/playlists", headers=AUTH).json()]
    assert names == ["ok"]
    assert client.get("/health", headers=AUTH).status_code == 200


def test_yo_and_ye_are_the_same_letter_when_matching():
    assert enrich.normalize("Ёлка") == enrich.normalize("Елка") == "елка"
    assert enrich.normalize("Мой") == "мои"  # й теряет кратку с обеих сторон одинаково


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("..", "Unknown"),
        ("???", "Unknown"),
        (".hidden", "hidden"),
        ("a\x00b\x1fc", "abc"),
        ("", "Unknown"),
    ],
)
def test_file_names_from_tags_are_safe(raw, expected):
    assert ingest.sanitize_filename(raw) == expected


def test_an_mp3_is_streamed_as_mp3(client, tmp_path):
    track = config.LIBRARY / "A" / "Singles" / "t.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"ID3" + b"\x00" * 100)
    expires = 9999999999
    response = client.get(
        "/api/stream",
        params={
            "path": "A/Singles/t.mp3",
            "exp": expires,
            "sig": signing.sign("A/Singles/t.mp3", expires),
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")


def test_shuffle_size_is_clamped(client, monkeypatch):
    monkeypatch.setattr(
        library,
        "library_index",
        lambda: [
            {"path": f"t{i}.m4a", "artist": f"A{i}", "title": "T", "duration": 1} for i in range(10)
        ],
    )
    body = client.get("/api/shuffle", headers=AUTH, params={"size": -1, "mode": "plain"}).json()
    assert len(body["queue"]) == 1
