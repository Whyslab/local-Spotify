"""«Похожие треки» под подборкой (adder/playlist_similar.py, /api/playlists/{name}/similar)."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from adder import config, outside, playlist_similar, sonic


def row(path, artist, title=None):
    return {"path": path, "artist": artist, "title": title or path, "album": "", "duration": 200.0}


def space_of(vectors: dict[str, list[float]]) -> sonic.Space:
    paths = list(vectors)
    m = np.array([vectors[p] for p in paths], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    n = len(paths)
    return sonic.Space(
        paths=paths,
        matrix=m,
        energy=np.zeros(n),
        language=["other"] * n,
        cloud=np.zeros(n, dtype=int),
        centers=m[:1],
        cloud_language=["other"],
        neighbours=[[0]],
        sound_scale=0.5,
    )


ROWS = [
    row("a1", "A"),
    row("a2", "A"),
    row("b1", "B"),  # подборка: два «угла»
    row("near_a", "C"),
    row("near_b", "D"),
    row("far", "E"),
    row("c2", "C"),
    row("c3", "C"),
]
SPACE = space_of(
    {
        "a1": [1, 0, 0],
        "a2": [0.98, 0.1, 0],
        "b1": [0, 1, 0],
        "near_a": [0.97, 0.05, 0.05],
        "near_b": [0.05, 0.98, 0.0],
        "far": [0, 0, 1],
        "c2": [0.96, 0.04, 0.1],
        "c3": [0.95, 0.06, 0.1],
    }
)


def test_close_to_part_of_the_playlist_beats_the_average():
    """Трек, похожий на одну часть подборки, — выше «ни на что» (far)."""
    got = playlist_similar.from_library(["a1", "a2", "b1"], ROWS, SPACE)
    paths = [t["path"] for t in got]
    assert paths[-1] == "far"
    assert set(paths[:2]) <= {"near_a", "c2", "c3", "near_b"}
    assert "a1" not in paths and "b1" not in paths
    # Не больше двух у одного артиста: у C три похожих.
    assert sum(1 for t in got if t["artist"] == "C") == 2


def test_skipped_and_outside_tracks_are_not_offered():
    rows = [*ROWS, row("outside:abc", "Z")]
    got = playlist_similar.from_library(["a1", "a2", "b1"], rows, SPACE, skipped={"near_a"})
    paths = [t["path"] for t in got]
    assert "near_a" not in paths and "outside:abc" not in paths


def test_without_sound_the_playlist_artists_are_used():
    rows = [
        row("a1", "A"),
        row("a2", "A"),
        row("a3", "A"),
        row("b1", "B"),
        row("b2", "B"),
        row("x", "X"),
    ]
    got = playlist_similar.from_library(["a1", "a2", "b1"], rows, None, seed="pl")
    assert [t["path"] for t in got] == ["a3", "b2"]  # частый в подборке артист — первым


def test_a_huge_playlist_is_compared_with_a_steady_sample(monkeypatch):
    monkeypatch.setattr(playlist_similar, "MEMBERS_MAX", 2)
    first = playlist_similar.from_library(["a1", "a2", "b1"], ROWS, SPACE, seed="Monday")
    again = playlist_similar.from_library(["a1", "a2", "b1"], ROWS, SPACE, seed="Monday")
    assert first == again and first


def test_new_tracks_come_from_the_playlist_artists(monkeypatch):
    asked = {}

    def candidates(seeds, have, want, budget=0):
        asked["seeds"] = seeds
        return [
            {"key": "k1", "artist": "N1", "title": "t1", "duration": 180, "cover": "", "seed": "A"},
            {"key": "k2", "artist": "N1", "title": "t2", "duration": 180, "cover": "", "seed": "A"},
            {"key": "k3", "artist": "N2", "title": "t3", "duration": 180, "cover": "", "seed": "B"},
        ]

    monkeypatch.setattr(outside, "candidates", candidates)
    monkeypatch.setattr(outside, "remember", lambda found: None)
    monkeypatch.setattr(outside, "status", lambda key: "unknown")
    got = playlist_similar.from_outside(["a1", "a2", "b1"], ROWS)
    assert asked["seeds"] == ["A", "B"]
    # Один чужой артист — один трек.
    assert [t["path"] for t in got] == ["outside:k1", "outside:k3"]
    assert all(t["outside"] and t["external"] for t in got)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_the_endpoint_answers_for_a_playlist_and_404_for_none(client, monkeypatch):
    from adder import app as app_module

    assert client.get("/api/playlists/Нет такой/similar").status_code == 404
    client.post("/api/playlists", json={"name": "Проба"})
    monkeypatch.setattr(app_module.library, "library_index", lambda: ROWS)
    monkeypatch.setattr(app_module.sonic, "load_space", lambda rows: SPACE)
    monkeypatch.setattr(app_module.radio, "recently_skipped", lambda: set())
    from adder import playlists

    monkeypatch.setattr(
        playlists,
        "read",
        lambda name: type(
            "P", (), {"entries": [type("E", (), {"path": p})() for p in ("a1", "a2", "b1")]}
        )(),
    )
    data = client.get("/api/playlists/Проба/similar").json()
    assert data["by"] == "sound" and data["tracks"][-1]["path"] == "far"
    monkeypatch.setattr(app_module.playlist_similar, "from_outside", lambda m, r: 1 / 0)
    assert client.get("/api/playlists/Проба/similar", params={"part": "new"}).json() == {
        "tracks": []
    }
    assert client.get("/api/playlists/Проба/similar", params={"part": "x"}).status_code == 422


def test_a_featured_artist_and_a_second_version_count_too():
    """24.11.2026 на живой копии: шесть из двенадцати — с GONE.Fludd (то первым,
    то на фите), и одна песня в двух версиях рядом."""
    rows = [
        row("m1", "M"),
        row("m2", "M"),
        row("m3", "M"),
        row("x1", "IROH • GONE.Fludd"),
        row("x2", "GONE.Fludd • bbno$", "I CAN KICK IT (feat. bbno$)"),
        row("x3", "GONE.Fludd • bbno$", "I CAN KICK IT ( , 2020)"),
        row("x4", "Lottery Billz • GONE.Fludd"),
        row("y1", "Y"),
    ]
    got = playlist_similar.from_library(["m1", "m2", "m3"], rows, None, seed="s")
    assert got == []  # без звучания и общих артистов — нечего
    space = space_of(
        {
            "m1": [1, 0, 0],
            "m2": [0.99, 0.1, 0],
            "m3": [0.98, 0, 0.1],
            "x1": [0.97, 0.02, 0],
            "x2": [0.96, 0.03, 0],
            "x3": [0.95, 0.04, 0],
            "x4": [0.94, 0.05, 0],
            "y1": [0, 1, 0],
        }
    )
    got = [t["path"] for t in playlist_similar.from_library(["m1", "m2", "m3"], rows, space)]
    # Одна из двух версий «I CAN KICK IT»; x4 — был бы третьим с GONE.Fludd.
    assert len(got) == 3 and "x4" not in got and "y1" in got
    assert len({"x2", "x3"} & set(got)) == 1
