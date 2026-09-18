"""Находки извне: то, чего в фонотеке нет.

Наружу тесты не ходят. `similar` — единственное место, которое спрашивает
Deezer, поэтому он подменяется целиком: иначе прогон зависел бы от того, что
сегодня ответил чужой сервис, и падал бы в самолёте.
"""

import pytest
from fastapi.testclient import TestClient

from adder import config, runtime, shelves, similar


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Своё приложение на временных путях: фикстура из test_app.py не общая."""
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


def _library() -> list[dict]:
    """Фонотека из одного любимого артиста — этого хватает для «любимцев»."""
    return [{"artist": "Любимый", "title": f"Своя песня {i}", "album": "Альбом"} for i in range(5)]


@pytest.fixture
def deezer(monkeypatch):
    """Похожие артисты и их чарты — без сети."""

    def similar_artists(artist, cache_dir, limit=12):
        return ["Сосед"] if artist == "Любимый" else []

    def top_tracks(artist, cache_dir, limit=5):
        if artist != "Сосед":
            return []
        return [
            {"artist": "Сосед", "title": "Чужая песня"},
            # Уже есть в фонотеке — не находка, хотя чарт её и называет.
            {"artist": "Любимый", "title": "Своя песня 1"},
        ]

    monkeypatch.setattr(similar, "similar_artists", similar_artists)
    monkeypatch.setattr(similar, "top_tracks", top_tracks)


def test_external_skips_what_the_library_already_has(deezer):
    found = shelves.external(_library(), want=10)

    assert {(t["artist"], t["title"]) for t in found} == {("Сосед", "Чужая песня")}


def test_external_survives_a_silent_network(monkeypatch):
    """Deezer не ответил — значит находок нет, а не ошибка на всю очередь."""
    monkeypatch.setattr(similar, "similar_artists", lambda *a, **kw: [])
    monkeypatch.setattr(similar, "top_tracks", lambda *a, **kw: [])

    assert shelves.external(_library(), want=10) == []


def test_external_needs_a_library_to_start_from(deezer):
    """Без своих треков не от чего отталкиваться — предлагать наугад нечего."""
    assert shelves.external([], want=10) == []


def test_endpoint_requires_a_token(client):
    assert client.get("/api/discover-external").status_code == 401


def test_endpoint_answers_with_tracks(client, auth, deezer):
    response = client.get("/api/discover-external?limit=5", headers=auth)

    assert response.status_code == 200
    assert isinstance(response.json()["tracks"], list)
