"""Связки перехода «как диджей»: когда служба их делает, где хранит и кому отдаёт.

Само сведение — tests/test_transition_render.py. Здесь скрипт подменён: тест
проверяет очередь, кэш и правила, а не librosa.
"""

import json
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from adder import config, covers, db, library, lyrics, navidrome, playlists, runtime, transitions


def _write_m4a(path, seconds=70, album="", number=None, title=None, gain=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", str(seconds), "-c:a", "aac", str(path)],
        check=True,
    )  # fmt: skip
    tags = MP4(path)
    tags["\xa9nam"] = [title or path.stem]
    tags["\xa9ART"] = ["Артист"]
    if album:
        tags["\xa9alb"] = [album]
        tags["aART"] = ["Артист"]
    if number:
        tags["trkn"] = [(number, 10)]
    if gain is not None:
        from mutagen.mp4 import MP4FreeForm

        tags["----:com.apple.iTunes:replaygain_track_gain"] = [
            MP4FreeForm(f"{gain:.2f} dB".encode())
        ]
    tags.save()


A = "Артист/Singles/Первый.m4a"
B = "Артист/Singles/Второй.m4a"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(runtime, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(runtime, "TRANSITIONS_DIR", tmp_path / "transitions")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(lyrics, "CACHE_DIR", tmp_path / "lyrics-cache")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True)
    _write_m4a(config.LIBRARY / A, gain=-4.0)
    _write_m4a(config.LIBRARY / B)
    library.invalidate_library_index()
    transitions.reset()

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client
    transitions.reset()


@pytest.fixture()
def renders(monkeypatch):
    """Скрипт сведения подменён: пишет «связку» и возвращает план; задания — в списке."""
    jobs = []

    def fake(job):
        jobs.append(job)
        with open(job["out"], "wb") as f:
            f.write(b"fLaC" + b"\0" * 100)
        return {"version": transitions.RENDER_VERSION, "case": "beatmatch", "lead": 4.0}

    monkeypatch.setattr(transitions, "_render", fake)
    monkeypatch.setattr(transitions, "_available", lambda: True)
    return jobs


def ask(client, a=A, b=B):
    r = client.post("/api/transitions", json={"from": a, "to": b})
    assert r.status_code == 200, r.text
    return r.json()


def wait_ready(client, a=A, b=B, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        answer = ask(client, a, b)
        if answer["status"] != "pending":
            return answer
        time.sleep(0.05)
    raise AssertionError("связка так и не собралась")


def test_a_bridge_is_made_once_and_then_served(client, renders):
    first = ask(client)
    assert first["status"] == "pending"
    ready = wait_ready(client)
    assert ready["status"] == "ready"
    assert ready["plan"]["case"] == "beatmatch"
    audio = client.get(f"/api/transitions/{ready['key']}.flac")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/flac"
    assert audio.content.startswith(b"fLaC")
    # Готовая связка не собирается заново.
    assert ask(client)["status"] == "ready"
    assert len(renders) == 1


def test_the_job_carries_tempo_voice_and_gain(client, renders):
    db.db_exec(
        "INSERT INTO audio_features(path, sha256, tempo, energy, brightness) VALUES (?, ?, ?, ?, ?)",
        (A, "x", 161.5, 0.1, 1000.0),
    )
    lyrics.CACHE_DIR.mkdir(parents=True)
    lyrics._key(B).write_text(
        json.dumps(
            {"found": True, "synced": [{"at": 0.0, "line": ""}, {"at": 11.5, "line": "Слово"}]}
        ),
        encoding="utf-8",
    )
    wait_ready(client)
    job = renders[0]
    assert job["tempo_a"] == 161.5 and job["tempo_b"] is None
    # Голос — первая непустая строка: пустая в начале — это проигрыш.
    assert job["voice_b"] == 11.5
    assert job["gain_a"] == pytest.approx(-4.0) and job["gain_b"] is None
    assert job["a"] == str(config.LIBRARY / A) and job["b"] == str(config.LIBRARY / B)


def test_album_neighbours_are_not_mixed(client, renders):
    one = "Артист/Альбом/01.m4a"
    two = "Артист/Альбом/02.m4a"
    _write_m4a(config.LIBRARY / one, album="Альбом", number=1)
    _write_m4a(config.LIBRARY / two, album="Альбом", number=2)
    library.invalidate_library_index()
    answer = ask(client, one, two)
    assert answer == {"status": "none", "reason": "album"}
    # Не по порядку — уже не соседи: переход можно.
    assert ask(client, two, one)["status"] == "pending"


def test_a_single_is_not_an_album(client, renders):
    # У сингла альбом называется как трек: два сингла подряд — не альбом.
    one = "Артист/Singles/Песня.m4a"
    _write_m4a(config.LIBRARY / one, album="Песня", number=1, title="Песня")
    _write_m4a(config.LIBRARY / B, album="Второй", number=2, title="Второй")
    library.invalidate_library_index()
    assert ask(client, one, B)["status"] == "pending"


def test_tracks_outside_the_library_and_short_ones_are_not_mixed(client, renders):
    assert ask(client, A, "outside:abc")["status"] == "none"
    short = "Артист/Singles/Короткий.m4a"
    _write_m4a(config.LIBRARY / short, seconds=20)
    library.invalidate_library_index()
    assert ask(client, short, B) == {"status": "none", "reason": "short"}


def test_a_changed_file_gets_a_new_bridge(client, renders):
    first = wait_ready(client)
    later = time.time() + 5
    os.utime(config.LIBRARY / B, (later, later))
    library.invalidate_library_index()
    assert ask(client)["status"] == "pending"
    second = wait_ready(client)
    assert second["key"] != first["key"]


def test_a_failed_render_is_not_retried_at_once(client, monkeypatch):
    calls = []

    def broken(job):
        calls.append(job)
        raise transitions.RenderError("librosa нет")

    monkeypatch.setattr(transitions, "_render", broken)
    monkeypatch.setattr(transitions, "_available", lambda: True)
    assert ask(client)["status"] == "pending"
    answer = wait_ready(client)
    assert answer == {"status": "none", "reason": "failed"}
    assert ask(client) == {"status": "none", "reason": "failed"}
    assert len(calls) == 1


def test_the_bridge_needs_the_token_and_a_real_key(client, renders):
    ready = wait_ready(client)
    fresh = TestClient(client.app)
    assert fresh.get(f"/api/transitions/{ready['key']}.flac").status_code == 401
    assert client.get("/api/transitions/..%2Fadder.flac").status_code == 404
    assert client.get("/api/transitions/" + "0" * 64 + ".flac").status_code == 404


def test_paths_outside_the_library_are_refused(client, renders):
    r = client.post("/api/transitions", json={"from": "../../etc/passwd", "to": B})
    assert r.status_code == 400


def test_old_bridges_are_pruned(client, renders, monkeypatch):
    monkeypatch.setattr(transitions, "KEEP", 2)
    paths = []
    for i in range(4):
        p = f"Артист/Singles/Т{i}.m4a"
        _write_m4a(config.LIBRARY / p)
        paths.append(p)
    library.invalidate_library_index()
    for i in range(3):
        wait_ready(client, paths[i], paths[i + 1])
        time.sleep(0.02)
    left = sorted(runtime.TRANSITIONS_DIR.glob("*.flac"))
    assert len(left) == 2
    assert len(list(runtime.TRANSITIONS_DIR.glob("*.json"))) == 2


def test_without_the_analysis_packages_there_is_no_bridge(client, monkeypatch):
    monkeypatch.setattr(transitions, "_available", lambda: False)
    assert ask(client) == {"status": "none", "reason": "unavailable"}


def test_render_version_matches_the_script():
    from pathlib import Path

    script = Path(transitions.__file__).resolve().parents[1] / "scripts" / "render_transition.py"
    source = script.read_text(encoding="utf-8")
    assert f"VERSION = {transitions.RENDER_VERSION}\n" in source


def test_a_request_left_without_a_worker_is_picked_up(client, renders, monkeypatch):
    """Поток ушёл (30 с тишины) ровно когда пришла просьба: она в очереди, а
    работать некому. Следующая же просьба той же пары должна поднять поток."""
    real = transitions._ensure_worker
    monkeypatch.setattr(transitions, "_ensure_worker", lambda: None)
    assert ask(client)["status"] == "pending"
    monkeypatch.setattr(transitions, "_ensure_worker", real)
    assert wait_ready(client, timeout=3)["status"] == "ready"
