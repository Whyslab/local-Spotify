"""Картинки из Deezer и шапки артистов (02.10.2026).

Сеть подменяется: `httpx.get` у webcovers и `_get` у similar — единственные
места, где эти модули ходят наружу.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from adder import artist_photos, config, runtime, shelves, similar, webcovers

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
COVER = "https://cdn-images.dzcdn.net/images/cover/abc/500x500-000000-80-0-0.jpg"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-secret"}


@pytest.fixture
def cdn(monkeypatch):
    """CDN Deezer без сети: отвечает картинкой и считает, сколько раз спросили."""
    asked = []

    def get(url, **kwargs):
        asked.append(url)
        return httpx.Response(200, content=JPEG, request=httpx.Request("GET", url))

    monkeypatch.setattr(webcovers.httpx, "get", get)
    return asked


# --- webcovers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url, ok",
    [
        (COVER, True),
        ("https://e-cdns-images.dzcdn.net/images/artist/x/1000x1000.jpg", True),
        ("http://cdn-images.dzcdn.net/a.jpg", False),  # не https
        ("https://example.com/a.jpg", False),
        ("https://dzcdn.net.evil.com/a.jpg", False),
        ("https://evil.com/?u=cdn-images.dzcdn.net", False),
        ("", False),
    ],
)
def test_only_deezer_cdn_over_https_is_fetched(url, ok):
    assert webcovers.allowed(url) is ok


def test_a_picture_is_fetched_once_then_served_from_disk(cdn):
    first = webcovers.fetch(COVER)
    second = webcovers.fetch(COVER)

    assert first == second == (JPEG, "image/jpeg")
    assert cdn == [COVER]


def test_a_foreign_address_never_reaches_the_network(cdn):
    assert webcovers.fetch("https://example.com/a.jpg") is None
    assert cdn == []


def test_something_that_is_not_a_picture_is_refused(monkeypatch):
    monkeypatch.setattr(
        webcovers.httpx,
        "get",
        lambda url, **kw: httpx.Response(200, content=b"<html>", request=httpx.Request("GET", url)),
    )
    assert webcovers.fetch(COVER) is None
    assert not runtime.WEB_COVERS_DIR.exists() or not any(runtime.WEB_COVERS_DIR.iterdir())


def test_the_cache_keeps_only_the_most_recently_shown(cdn, monkeypatch):
    monkeypatch.setattr(webcovers, "MAX_FILES", 10)
    for i in range(12):
        webcovers.fetch(f"https://cdn-images.dzcdn.net/images/cover/{i}/500x500.jpg")

    assert len(list(runtime.WEB_COVERS_DIR.iterdir())) <= 10


# --- фото артиста из Deezer ---------------------------------------------------


def _artist(
    name, picture="https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg", fans=1, albums=5
):
    return {"name": name, "picture_xl": picture, "nb_fan": fans, "nb_album": albums}


@pytest.fixture
def search(monkeypatch):
    """Поиск артистов Deezer: что вернуть — задаёт тест."""
    answer: dict = {"rows": [], "calls": 0}

    def get(client, url, params):
        answer["calls"] += 1
        if isinstance(answer["rows"], Exception):
            raise answer["rows"]
        return answer["rows"]

    monkeypatch.setattr(similar, "_get", get)
    return answer


def test_the_photo_of_the_artist_with_that_name(search, tmp_path):
    search["rows"] = [
        _artist("Pharaoh Sanders", "https://cdn-images.dzcdn.net/images/artist/jazz/1.jpg", 900),
        _artist("PHARAOH", "https://cdn-images.dzcdn.net/images/artist/rap/1.jpg", 50),
    ]
    assert similar.artist_picture("Pharaoh", tmp_path).endswith("/rap/1.jpg")


def test_a_cyrillic_name_finds_its_latin_spelling(search, tmp_path):
    search["rows"] = [
        _artist("Monetochka", "https://cdn-images.dzcdn.net/images/artist/lat/1.jpg"),
    ]
    assert similar.artist_picture("Монеточка", tmp_path).endswith("/lat/1.jpg")


def test_an_empty_exact_entry_gives_way_to_the_latin_spelling(search, tmp_path):
    """«Алина Орлова» кириллицей у Deezer — пустышка без релизов, а её песни
    («Летели облака») — под «Alina Orlova»."""
    search["rows"] = [
        _artist("Alina Orlova", "https://cdn-images.dzcdn.net/images/artist/lt/1.jpg", 12000),
        _artist("Алина Орлова", "https://cdn-images.dzcdn.net/images/artist//1.jpg", 300, albums=0),
    ]
    assert similar.artist_picture("Алина Орлова", tmp_path).endswith("/lt/1.jpg")


def test_a_real_exact_entry_without_photo_does_not_borrow_a_namesakes(search, tmp_path):
    search["rows"] = [
        _artist("Boris", "https://cdn-images.dzcdn.net/images/artist/other/1.jpg", 9000),
        _artist("Борис", "https://cdn-images.dzcdn.net/images/artist//1.jpg", 40, albums=3),
    ]
    assert similar.artist_picture("Борис", tmp_path) == ""


def test_no_photo_rather_than_a_stranger(search, tmp_path):
    """Тёзка или заглушка Deezer — не фото этого артиста."""
    search["rows"] = [
        _artist("Someone Else"),
        _artist("Quiet", "https://cdn-images.dzcdn.net/images/artist//1000x1000.jpg"),
    ]
    assert similar.artist_picture("Quiet", tmp_path) == ""


def test_the_answer_is_remembered(search, tmp_path):
    search["rows"] = [_artist("Lizer")]
    similar.artist_picture("Lizer", tmp_path)
    similar.artist_picture("Lizer", tmp_path)
    assert search["calls"] == 1


def test_a_silent_network_is_not_remembered_as_no_photo(search, tmp_path):
    search["rows"] = similar._Unreachable("ответ 503")
    assert similar.artist_picture("Lizer", tmp_path) == ""
    search["rows"] = [_artist("Lizer")]
    assert similar.artist_picture("Lizer", tmp_path) != ""


# --- своя шапка ---------------------------------------------------------------


def test_own_photo_is_stored_by_name_whatever_the_case():
    artist_photos.store("PHARAOH", PNG)
    assert artist_photos.read("pharaoh") == (PNG, "image/png")
    artist_photos.store("Pharaoh", JPEG)  # другой формат вытесняет прежний
    assert artist_photos.read("PHARAOH") == (JPEG, "image/jpeg")
    artist_photos.delete("pharaoh")
    assert artist_photos.read("PHARAOH") is None


def test_a_name_with_slashes_stays_inside_its_folder():
    artist_photos.store("../../AC/DC", PNG)
    assert [p.parent for p in runtime.ARTIST_PHOTOS_DIR.iterdir()] == [runtime.ARTIST_PHOTOS_DIR]


# --- адреса службы ------------------------------------------------------------


def test_picture_endpoints_require_a_token(client):
    assert client.get("/api/web-cover", params={"url": COVER}).status_code == 401
    assert client.get("/api/artist-photo", params={"name": "X"}).status_code == 401
    assert client.post("/api/artist-photo", params={"name": "X"}).status_code == 401


def test_web_cover_refuses_a_foreign_address(client, auth, cdn):
    response = client.get(
        "/api/web-cover", params={"url": "https://example.com/a.jpg"}, headers=auth
    )
    assert response.status_code == 404
    assert cdn == []


def test_web_cover_serves_a_deezer_picture(client, auth, cdn):
    response = client.get("/api/web-cover", params={"url": COVER}, headers=auth)
    assert response.status_code == 200
    assert response.content == JPEG


def test_own_header_wins_over_deezer_and_can_be_dropped(client, auth, monkeypatch, cdn):
    monkeypatch.setattr(similar, "artist_picture", lambda name, cache: COVER)

    assert client.get("/api/artist-photo", params={"name": "Lizer"}, headers=auth).content == JPEG
    assert (
        client.get("/api/artist-photo/info", params={"name": "Lizer"}, headers=auth).json()["own"]
        is False
    )

    upload = client.post(
        "/api/artist-photo",
        params={"name": "Lizer"},
        files={"image": ("own.png", PNG, "image/png")},
        headers=auth,
    )
    assert upload.status_code == 200
    assert client.get("/api/artist-photo", params={"name": "Lizer"}, headers=auth).content == PNG
    assert (
        client.get("/api/artist-photo/info", params={"name": "lizer"}, headers=auth).json()["own"]
        is True
    )

    client.delete("/api/artist-photo", params={"name": "Lizer"}, headers=auth)
    assert client.get("/api/artist-photo", params={"name": "Lizer"}, headers=auth).content == JPEG


def test_no_photo_anywhere_is_a_404(client, auth, monkeypatch):
    monkeypatch.setattr(similar, "artist_picture", lambda name, cache: "")
    response = client.get("/api/artist-photo", params={"name": "Nobody"}, headers=auth)
    assert response.status_code == 404


def test_an_upload_that_is_not_a_picture_is_refused(client, auth):
    response = client.post(
        "/api/artist-photo",
        params={"name": "Lizer"},
        files={"image": ("x.png", b"not a picture", "image/png")},
        headers=auth,
    )
    assert response.status_code == 415
    assert artist_photos.read("Lizer") is None


# --- «Новое для вас» ----------------------------------------------------------


@pytest.fixture
def many_finds(monkeypatch):
    """Двадцать соседей по три трека — есть из чего выбирать разные наборы."""
    neighbours = [f"Сосед {i}" for i in range(20)]
    monkeypatch.setattr(
        similar, "similar_artists", lambda a, c, limit=12: neighbours if a == "Любимый" else []
    )
    monkeypatch.setattr(
        similar,
        "top_tracks",
        lambda a, c, limit=5: [
            {
                "artist": a,
                "title": f"{a} — песня {n}",
                "cover": f"https://cdn-images.dzcdn.net/{a}{n}",
            }
            for n in range(3)
        ],
    )


def _library():
    return [{"artist": "Любимый", "title": f"Своя {i}", "album": "А"} for i in range(5)]


def test_finds_carry_their_cover(many_finds):
    found = shelves.external(_library(), want=12)
    assert all(t["cover"].startswith("https://cdn-images.dzcdn.net/") for t in found)


def test_each_seed_shows_another_set_and_no_seed_stays_put(many_finds):
    def titles(seed=None):
        return [t["title"] for t in shelves.external(_library(), want=12, seed=seed)]

    assert titles() == titles()
    assert titles(1) == titles(1)
    assert titles(1) != titles(2)


def test_the_endpoint_answers_a_new_set_each_time(client, auth, many_finds, monkeypatch):
    from adder import library

    monkeypatch.setattr(library, "library_index", _library)
    sets = {
        tuple(
            t["title"] for t in client.get("/api/discover-external", headers=auth).json()["tracks"]
        )
        for _ in range(4)
    }
    assert len(sets) > 1
