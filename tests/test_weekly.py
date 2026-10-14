"""Подборка недели (план 4c.3).

Deezer не спрашивается: ``outside.candidates`` подменён (conftest глушит его
в пустоту, здесь — список находок).
"""

import datetime
import json

import pytest

from adder import db, outside, runtime, weekly

MONDAY = datetime.datetime(2026, 10, 12, 4, 30).timestamp()
DAY = 86400


def row(artist, n):
    return {"path": f"{artist}/{n}.m4a", "artist": artist, "title": f"{artist}{n}"}


LIBRARY = [row("Big", i) for i in range(5)] + [row("Mid", i) for i in range(3)] + [row("Heard", 0)]


def finds(n, artists=20):
    return [
        {
            "key": f"{i:016x}",
            "artist": f"New{i % artists}",
            "title": f"N{i}",
            "duration": 180,
            "album": "",
            "cover": "",
            "seed": "Big",
        }
        for i in range(n)
    ]


@pytest.fixture
def deezer(monkeypatch):
    asked = {}

    def candidates(seed_artists, have, want, budget=0):
        asked.update(seeds=list(seed_artists), want=want, budget=budget)
        return asked.get("answer", finds(100))[:want]

    monkeypatch.setattr(outside, "candidates", candidates)
    return asked


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "weekly.db")
    db.db_init()

    def play(path, days_ago=1, skipped=False):
        db.db_exec(
            "INSERT INTO plays(path, played_at, skipped) "
            "VALUES(?, datetime('now', 'localtime', ?), ?)",
            (path, f"-{days_ago} days", 1 if skipped else 0),
        )

    return play


def test_a_week_runs_monday_to_sunday():
    assert weekly.week_of(MONDAY) == "2026-W42"
    assert weekly.week_of(MONDAY + 6 * DAY + 3600 * 19) == "2026-W42"
    assert weekly.week_of(MONDAY + 7 * DAY) == "2026-W43"


def test_it_is_due_once_a_week(deezer, journal):
    assert weekly.due(MONDAY)
    weekly.build(LIBRARY, now=MONDAY)
    assert not weekly.due(MONDAY + 3 * DAY)
    assert weekly.due(MONDAY + 7 * DAY)


def test_seeds_are_who_was_heard_then_the_biggest(deezer, journal):
    for _ in range(3):
        journal("Heard/0.m4a")
    journal("Mid/0.m4a", skipped=True)  # пропуск — не «слушали»
    journal("Mid/1.m4a", days_ago=40)  # давно
    assert weekly.favourites(LIBRARY)[:3] == ["Heard", "Big", "Mid"]


def test_twenty_five_new_tracks_at_most_two_per_artist(deezer, journal):
    report = weekly.build(LIBRARY, now=MONDAY)
    assert report == {"built": True, "week": "2026-W42", "count": 25}
    tracks = weekly.current()["tracks"]
    assert len(tracks) == 25
    artists = [t["artist"] for t in tracks]
    assert max(artists.count(a) for a in artists) == 2
    assert all(t["path"].startswith(outside.PREFIX) and t["external"] for t in tracks)
    # Можно просить скачать.
    assert {t["state"] for t in tracks} == {"wanted"}
    # Ночью подбор может ждать Deezer дольше перемешивания.
    assert deezer["budget"] == weekly.BUDGET


def test_what_was_offered_before_is_not_offered_again(deezer, journal):
    weekly.build(LIBRARY, now=MONDAY)
    first = {t["path"] for t in weekly.current()["tracks"]}
    deezer["answer"] = finds(100, artists=50)
    weekly.build(LIBRARY, now=MONDAY + 7 * DAY)
    second = {t["path"] for t in weekly.current()["tracks"]}
    assert second and not first & second


def test_history_is_kept_for_half_a_year(deezer, journal):
    for week in range(30):
        deezer["answer"] = [
            dict(f, key=f"{week:04x}{i:012x}") for i, f in enumerate(finds(100, artists=50))
        ]
        weekly.build(LIBRARY, now=MONDAY + week * 7 * DAY)
    history = json.loads(runtime.WEEKLY_FILE.read_text(encoding="utf-8"))["history"]
    assert len(history) == weekly.HISTORY_WEEKS
    assert max(history) == weekly.week_of(MONDAY + 29 * 7 * DAY)


def test_a_silent_deezer_keeps_last_weeks_selection(deezer, journal):
    weekly.build(LIBRARY, now=MONDAY)
    deezer["answer"] = []
    report = weekly.build(LIBRARY, now=MONDAY + 7 * DAY)
    assert report == {"built": False, "week": "2026-W42", "count": 25}
    assert len(weekly.current()["tracks"]) == 25
    # И завтра попробуем снова.
    assert weekly.due(MONDAY + 8 * DAY)


def test_a_candidate_the_cache_forgot_is_remembered_again(deezer, journal):
    weekly.build(LIBRARY, now=MONDAY)
    key = outside.track_key(weekly.current()["tracks"][0]["path"])
    outside._meta_path(key).unlink()
    assert outside.status(key) == "unknown"
    assert weekly.current()["tracks"][0]["state"] == "wanted"


def test_the_first_tracks_not_downloaded_are_asked_for(deezer, journal, monkeypatch):
    asked = []
    monkeypatch.setattr(outside, "request", lambda keys, keep=False: asked.append(keys))
    weekly.build(LIBRARY, now=MONDAY)
    tracks = weekly.current()["tracks"]
    tracks[0]["state"] = "ready"
    tracks[1]["state"] = "pending"
    weekly.prefetch(tracks)
    assert asked == [[outside.track_key(t["path"]) for t in tracks[2:7]]]


def test_a_broken_file_is_no_selection():
    runtime.WEEKLY_FILE.write_text("[1, 2", encoding="utf-8")
    assert weekly.current() == {"week": None, "tracks": []}
    assert weekly.due(MONDAY)


def test_home_gives_the_selection_and_asks_for_its_first_tracks(deezer, monkeypatch):
    from fastapi.testclient import TestClient

    from adder import app as app_module
    from adder import config, library, navidrome, shelves

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    monkeypatch.setattr(library, "library_index", lambda: LIBRARY)
    # «Похоже на любимое» спрашивает Deezer — здесь не про неё.
    monkeypatch.setattr(shelves, "discover", lambda rows, seed=None: {"tracks": [], "based_on": []})
    asked = []
    monkeypatch.setattr(outside, "request", lambda keys, keep=False: asked.append(keys))
    with TestClient(app_module.app) as client:
        weekly.build(LIBRARY, now=MONDAY)
        body = client.get("/api/home", headers={"Authorization": "Bearer test-secret"}).json()

    assert body["weekly"]["week"] == "2026-W42"
    assert len(body["weekly"]["tracks"]) == 25
    assert len(asked) == 1 and len(asked[0]) == weekly.PREFETCH


def _export_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "export_shelves.py"
    spec = importlib.util.spec_from_file_location("export_shelves_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_night_builds_it_only_when_a_new_week_came(deezer, journal, capsys, monkeypatch):
    from types import SimpleNamespace

    library = SimpleNamespace(library_index=lambda: LIBRARY)
    script = _export_script()
    script.weekly_selection(library, weekly)
    assert "Подборка недели " in capsys.readouterr().out

    built = []
    monkeypatch.setattr(weekly, "build", lambda rows: built.append(rows))
    script.weekly_selection(library, weekly)
    assert built == []
