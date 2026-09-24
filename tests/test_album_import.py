"""Whole albums: by Deezer link or by name, each track found on YouTube strictly."""

import pytest
from fastapi.testclient import TestClient

from adder import config, db, enrich, runtime, sources

AUTH = {"Authorization": "Bearer test-secret"}

ALBUM = {
    "id": 12047952,
    "title": "OK Computer",
    "artist": {"name": "Radiohead"},
    "nb_tracks": 3,
    "release_date": "1997-05-21",
    "cover_xl": "https://cdn/ok.jpg",
}
TRACKS = {
    "data": [
        {
            "id": 1,
            "title": "Airbag",
            "duration": 284,
            "track_position": 1,
            "artist": {"name": "Radiohead"},
        },
        {
            "id": 2,
            "title": "Paranoid Android",
            "duration": 387,
            "track_position": 2,
            "artist": {"name": "Radiohead"},
        },
        {
            "id": 3,
            "title": "Unreleased Demo",
            "duration": 200,
            "track_position": 3,
            "artist": {"name": "Radiohead"},
        },
    ]
}


def deezer(routes):
    def get(url):
        for prefix, body in routes.items():
            if url.startswith(enrich.DEEZER + prefix):
                return body
        return None

    return get


@pytest.mark.parametrize(
    ("url", "album_id"),
    [
        ("https://www.deezer.com/album/12047952", "12047952"),
        ("https://www.deezer.com/fr/album/12047952?utm=x", "12047952"),
        ("https://deezer.com/en-gb/album/12047952/", "12047952"),
        ("https://www.deezer.com/track/12047952", None),
        ("https://evil.example/album/1", None),
    ],
)
def test_deezer_album_links_are_recognised(url, album_id):
    assert sources.deezer_album_id(url) == album_id


def test_album_search_returns_choices(monkeypatch):
    found = {
        "data": [
            {
                "id": 1,
                "title": "OK Computer",
                "artist": {"name": "Radiohead"},
                "nb_tracks": 12,
                "cover_medium": "https://cdn/c.jpg",
                "record_type": "album",
            },
            {"title": "no id"},
        ]
    }
    monkeypatch.setattr(enrich, "_get", deezer({"/search/album": found}))

    (album,) = sources.search_albums("radiohead ok computer")

    assert album == {
        "id": "1",
        "title": "OK Computer",
        "artist": "Radiohead",
        "tracks": 12,
        "cover": "https://cdn/c.jpg",
        "type": "album",
    }
    assert sources.search_albums("   ") == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from adder import app as app_module

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    runtime.PROCESSING_URLS.clear()
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
        runtime.TASK_QUEUE.task_done()
    monkeypatch.setattr(
        enrich, "_get", deezer({"/album/12047952/tracks": TRACKS, "/album/12047952": ALBUM})
    )

    def youtube(candidate):
        if candidate.title == "Unreleased Demo":
            return None  # nothing on YouTube passes the strict match
        slug = candidate.title.replace(" ", "")[:11].ljust(11, "x")
        return {"url": f"https://www.youtube.com/watch?v={slug}"}

    monkeypatch.setattr(sources, "best_youtube_match", youtube)
    with TestClient(app_module.app) as test_client:
        yield test_client
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
        runtime.TASK_QUEUE.task_done()


def test_an_album_is_queued_with_its_id_on_every_task(client):
    response = client.post("/api/import-album", json={"id": "12047952"}, headers=AUTH)

    body = response.json()
    assert response.status_code == 200
    assert body["album"] == {"id": "12047952", "title": "OK Computer", "artist": "Radiohead"}
    assert (body["read"], body["queued"]) == (3, 2)
    assert body["unmatched"] == [{"artist": "Radiohead", "title": "Unreleased Demo"}]
    hints = {r["album_hint"] for r in db.db_query("SELECT album_hint FROM tasks")}
    assert hints == {"12047952"}


def test_a_deezer_album_link_goes_through_the_playlist_import(client):
    response = client.post(
        "/api/import-playlist", json={"url": "https://www.deezer.com/album/12047952"}, headers=AUTH
    )

    assert response.json()["source"] == "deezer-album"
    assert response.json()["queued"] == 2


def test_album_endpoints_need_the_token_and_a_numeric_id(client):
    assert client.get("/api/albums/search", params={"q": "x"}).status_code == 401
    assert client.post("/api/import-album", json={"id": "12047952"}).status_code == 401
    assert client.post("/api/import-album", json={"id": "../x"}, headers=AUTH).status_code == 400


def test_tracks_are_tagged_from_the_chosen_album_not_a_per_track_guess(monkeypatch):
    full = {"id": 2, "track_position": 2, "disk_number": 1, "contributors": [{"name": "Radiohead"}]}
    monkeypatch.setattr(
        enrich,
        "_get",
        deezer({"/album/12047952/tracks": TRACKS, "/album/12047952": ALBUM, "/track/2": full}),
    )
    # A per-track lookup would have found the single; it must not be asked.
    monkeypatch.setattr(
        enrich, "lookup", lambda a, t: enrich.TrackInfo(album="Paranoid Android (Single)")
    )

    info, source = enrich.describe("Radiohead", "Paranoid Android", album_id="12047952")

    assert source == "deezer"
    assert info.album == "OK Computer"
    assert (info.track_number, info.track_total) == (2, 3)
    assert info.cover_url == "https://cdn/ok.jpg"


def test_a_title_missing_from_the_album_falls_back_to_the_usual_lookup(monkeypatch):
    monkeypatch.setattr(enrich, "_get", deezer({"/album/12047952/tracks": TRACKS}))
    monkeypatch.setattr(enrich, "lookup", lambda a, t: enrich.TrackInfo(album="Elsewhere"))

    info, _ = enrich.describe("Radiohead", "Creep", album_id="12047952")

    assert info.album == "Elsewhere"


def test_a_youtube_music_album_link_is_a_playlist(monkeypatch, client):
    # music.youtube.com albums are playlists (list=OLAK5uy_...): yt-dlp lists them.
    monkeypatch.setattr(
        sources,
        "youtube_playlist",
        lambda url: [
            "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        ],
    )

    response = client.post(
        "/api/import-playlist",
        json={"url": "https://music.youtube.com/playlist?list=OLAK5uy_kVkEsCWLs"},
        headers=AUTH,
    )

    assert response.json()["source"] == "youtube"
    assert response.json()["queued"] == 2
