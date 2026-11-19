"""Новое умное в службе: /api/shuffle, настройка, радио, слепое сравнение,
подборки по настроению, статистика, расчёт звучания новых треков."""

import sqlite3
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from adder import analysis, config, db, library, navidrome, runtime, sonic, stats

DIM = 8


def _rows():
    rng = np.random.default_rng(0)
    index, sound = [], []
    for g in range(4):
        for a in range(5):
            artist = f"Артист{g}_{a}" if g % 2 == 0 else f"Artist{g}_{a}"
            for t in range(4):
                path = f"{artist}/Album/{t}.m4a"
                index.append(
                    {
                        "path": path,
                        "artist": artist,
                        "title": f"Песня {t}" if g % 2 == 0 else f"Song {t}",
                        "album": f"A{g}{a}",
                        "duration": 180.0,
                    }
                )
                v = np.zeros(DIM, np.float32)
                v[g] = 1
                v += rng.normal(0, 0.1, DIM).astype(np.float32)
                v /= np.linalg.norm(v)
                sound.append((path, v))
    return index, sound


INDEX, SOUND = _rows()


def _store_sound(paths=None):
    con = sqlite3.connect(runtime.DB_PATH)
    for path, v in SOUND:
        if paths is not None and path not in paths:
            continue
        con.execute(
            "INSERT OR REPLACE INTO sonic(path, sha256, version, embedding, styles, happy, sad, "
            "aggressive, relaxed, party, danceable, voice, arousal, valence) "
            "VALUES(?, 'h', 'effnet1', ?, ?, ?, ?, ?, ?, ?, ?, 0.9, ?, 5)",
            (
                path,
                v.tobytes(),
                np.zeros(4, np.float16).tobytes(),
                *(float(x) for x in np.random.default_rng(len(path)).random(6)),
                5.0 + len(path) % 3,
            ),
        )
    con.commit()
    con.close()
    sonic.forget()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)
    library.invalidate_library_index()
    monkeypatch.setattr(library, "library_index", lambda: [dict(r) for r in INDEX])
    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_smart_shuffle_uses_the_sound_engine_once_measured(client):
    body = client.get("/api/shuffle", params={"size": 40}).json()
    assert body["engine"] == "classic"  # звучания нет — старое умное
    _store_sound()
    body = client.get("/api/shuffle", params={"size": 40}).json()
    assert body["engine"] == "sonic"
    assert len(body["queue"]) == 40
    artists = [entry["artist"] for entry in body["queue"]]
    for i, artist in enumerate(artists):
        assert artist not in artists[max(0, i - 5) : i], (i, artists)


def test_partly_measured_library_stays_on_the_old_engine(client):
    _store_sound({path for path, _ in SOUND[:40]})  # половина
    assert client.get("/api/shuffle").json()["engine"] == "classic"


def test_the_setting_brings_the_old_engine_back(client):
    _store_sound()
    assert client.get("/api/settings").json() == {"outside_mix": "off", "smart_engine": "sonic"}
    r = client.put("/api/settings", json={"smart_engine": "classic"})
    assert r.json() == {"outside_mix": "off", "smart_engine": "classic"}
    assert client.get("/api/shuffle").json()["engine"] == "classic"
    # Одно поле меняет только себя.
    r = client.put("/api/settings", json={"outside_mix": "few"})
    assert r.json() == {"outside_mix": "few", "smart_engine": "classic"}
    assert client.put("/api/settings", json={"smart_engine": "x"}).status_code == 422


def test_playlist_shuffle_stays_inside_the_playlist(client):
    _store_sound()
    from adder import playlists

    names = [r["path"] for r in INDEX[:12]]
    playlists.create("Тест", names)
    body = client.get("/api/shuffle", params={"playlist": "Тест", "size": 50}).json()
    assert body["engine"] == "sonic"
    assert sorted(e["path"] for e in body["queue"]) == sorted(names)


def test_radio_goes_through_the_same_engine(client, monkeypatch):
    _store_sound()
    from adder import radio

    monkeypatch.setattr(radio, "station", lambda artist, need, tracks: {radio._name(artist)})
    calls = []
    real = sonic.build_session

    def spy(*args, **kwargs):
        calls.append(kwargs.get("focus_artist"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sonic, "build_session", spy)
    body = client.post("/api/radio", json={"artist": "Артист0_0", "size": 10}).json()
    assert body["queue"] and body["queue"][0]["artist"] == "Артист0_0"
    assert calls and calls[0] == "Артист0_0"


def test_blind_compares_new_and_old_and_counts_only_that(client):
    assert client.post("/api/shuffle/blind").status_code == 409
    _store_sound()
    db.db_exec(
        "INSERT INTO blind_trials(smart_side, choice) VALUES('A', 'A')"
    )  # старое: умное/обычное
    trial = client.post("/api/shuffle/blind", params={"size": 20}).json()
    rows = db.db_query("SELECT kind FROM blind_trials WHERE id = ?", (trial["trial"],))
    assert rows[0]["kind"] == "sonic"
    assert len(trial["A"]) == 20 and len(trial["B"]) == 20
    assert client.get("/api/shuffle/blind/results").json()["decided"] == 0
    client.post(f"/api/shuffle/blind/{trial['trial']}", json={"choice": "B"})
    assert client.get("/api/shuffle/blind/results").json()["decided"] == 1


def test_home_moods_come_from_sound(client):
    # Сама полка «Под настроение»: остальная главная ходит в Deezer.
    from adder import app as app_module

    keys = {m["key"] for m in app_module._mood_shelves([])}
    assert not keys & {"sad", "happy", "hard"}  # без звучания — как раньше
    _store_sound()
    moods = app_module._mood_shelves([])
    assert {m["key"] for m in moods} & {"sad", "happy", "hard"}
    for m in moods:
        paths = [t["path"] for t in m["tracks"]]
        assert len(paths) == len(set(paths))


def test_smart_skips_count_outside_tracks_too(client):
    """Только новое умное (engine='sonic'), вместе с чужими; старое умное,
    ручная игра и чужое не из умного — мимо."""
    db.db_exec(
        "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, mode, engine) "
        "VALUES('a', datetime('now','localtime'), 200, 200, 0, 'smart', 'sonic'), "
        "('b', datetime('now','localtime'), 3, 200, 1, 'manual', NULL), "
        "('c', datetime('now','localtime'), 3, 200, 1, 'smart', 'classic'), "
        "('d', datetime('now','localtime'), 3, 200, 1, 'smart', NULL)"
    )
    db.db_exec(
        "INSERT INTO outside_plays(key, played_at, played_seconds, skipped, mode, engine) "
        "VALUES('k', datetime('now','localtime'), 4, 1, 'smart', 'sonic'), "
        "('m', datetime('now','localtime'), 4, 1, 'manual', NULL)"
    )
    result = stats.smart_skips()
    assert (result["plays"], result["skips"], result["outside_plays"]) == (2, 1, 1)
    assert client.get("/api/stats").json()["smart_skips"]["share"] == 0.5


def test_new_track_gets_its_sound_right_away(monkeypatch, tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    for name in analysis.MODEL_NAMES:
        (models / name).write_bytes(b"")
    monkeypatch.setattr(analysis, "MODELS", models)
    commands = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(
        analysis.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or Done()
    )
    analysis._run("/lib/a.m4a")
    scripts = [next(part for part in cmd if part.endswith(".py")) for cmd in commands]
    assert [s.rsplit("/", 1)[-1] for s in scripts] == ["analyze_audio.py", "embed_audio.py"]
    assert "--models" in commands[1]


def test_without_models_only_tempo_is_measured(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis, "MODELS", tmp_path / "none")
    commands = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(
        analysis.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or Done()
    )
    analysis._run("/lib/a.m4a")
    assert len(commands) == 1


def test_old_blind_table_gets_the_kind_column(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "old.db")
    con = sqlite3.connect(runtime.DB_PATH)
    con.execute(
        "CREATE TABLE blind_trials(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, "
        "smart_side TEXT NOT NULL, choice TEXT, decided_at TEXT)"
    )
    con.execute("INSERT INTO blind_trials(smart_side, choice) VALUES('A', 'B')")
    con.commit()
    con.close()
    db.db_init()
    cols = [
        r[1] for r in sqlite3.connect(runtime.DB_PATH).execute("PRAGMA table_info(blind_trials)")
    ]
    assert "kind" in cols
    assert db.db_query("SELECT kind FROM blind_trials")[0]["kind"] is None


def test_partial_model_set_does_not_run_the_embedding(monkeypatch, tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / analysis.MODEL_NAMES[0]).write_bytes(b"")
    monkeypatch.setattr(analysis, "MODELS", models)
    assert not analysis._embedding_ready()


def test_play_keeps_the_engine_only_for_smart_and_radio(client):
    for path, mode in (("x/a.m4a", "smart"), ("x/b.m4a", "manual")):
        r = client.post(
            "/api/plays",
            json={
                "path": path,
                "played_seconds": 100,
                "duration": 200,
                "mode": mode,
                "engine": "sonic",
                "heard_at": time.time() - 300 - len(mode),
            },
        )
        assert r.status_code == 200, r.text
    rows = {r["path"]: r["engine"] for r in db.db_query("SELECT path, engine FROM plays")}
    assert rows == {"x/a.m4a": "sonic", "x/b.m4a": None}


@pytest.mark.parametrize("engine", ["sonic", "classic"])
def test_small_station_plays_first_and_the_rest_pads(client, monkeypatch, engine):
    """Круг станции меньше порции: раньше он выбрасывался целиком, и радио
    шло из всей фонотеки (разбор этапа 5) — теперь сначала вся станция."""
    _store_sound()
    from adder import radio

    client.put("/api/settings", json={"smart_engine": engine})
    monkeypatch.setattr(radio, "station", lambda artist, need, tracks: {radio._name(artist)})
    body = client.post("/api/radio", json={"artist": "Артист0_0", "size": 12}).json()
    station = {r["path"] for r in INDEX if r["artist"] == "Артист0_0"}
    assert 0 < len(station) < 12
    paths = [e["path"] for e in body["queue"] if not e.get("external")]
    assert len(paths) == 12
    assert set(paths[: len(station)]) == station
    assert {e["engine"] for e in body["queue"]} == {engine}


def test_queue_keeps_its_length_when_outside_only_fits_at_block_joints(client, monkeypatch):
    """Чужим — только стыки блоков; кому места нет, тех добирают своими,
    и очередь не короче заказанной (рецензия этапа 5: 24 из 30 вживую)."""
    from adder import outside

    _store_sound()
    client.put("/api/settings", json={"outside_mix": "on"})
    found = [
        {"key": f"k{i}", "artist": f"Чужой{i}", "title": f"Песня{i}", "seed": "x"}
        for i in range(20)
    ]
    monkeypatch.setattr(outside, "candidates", lambda *a, **k: list(found))
    remembered = []
    monkeypatch.setattr(outside, "remember", lambda items: remembered.extend(items))
    body = client.get("/api/shuffle", params={"size": 30}).json()
    assert body["engine"] == "sonic"
    assert len(body["queue"]) == 30
    external = [e for e in body["queue"] if e.get("external")]
    assert external and len(remembered) == len(external)
