"""«Недавно слушали» on the home page and the «Статистика» screen (/api/stats)."""

import datetime

import pytest
from fastapi.testclient import TestClient

from adder import config, db, library, navidrome, shelves

ROWS = [
    {"path": "A/a1.m4a", "artist": "Аня", "title": "Первая", "album": "X", "duration": 200.0},
    {"path": "A/a2.m4a", "artist": "Аня", "title": "Вторая", "album": "X", "duration": 180.0},
    {
        "path": "B/b1.m4a",
        "artist": "Борис • Аня",
        "albumartist": "Борис",
        "title": "Дуэт",
        "album": "Y",
        "duration": 240.0,
    },
    {"path": "C/c1.m4a", "artist": "Вера", "title": "Одна", "album": "", "duration": 100.0},
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)
    library.invalidate_library_index()
    monkeypatch.setattr(library, "library_index", lambda: [dict(r) for r in ROWS])
    # Остальные полки главной спрашивают Deezer о похожих артистах — здесь не нужны.
    monkeypatch.setattr(shelves, "discover", lambda rows, seed=None: {"tracks": [], "based_on": []})

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def at(days_ago: float = 0, hour: int = 12, minute: int = 0, second: int = 0) -> str:
    """played_at the way /api/plays writes it: local time, a space, no zone."""
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return f"{day.isoformat()} {hour:02d}:{minute:02d}:{second:02d}"


def play(path, played_at, seconds=180.0, skipped=False):
    db.db_exec(
        "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, source, mode) "
        "VALUES(?, ?, ?, 200, ?, 'player', 'manual')",
        (path, played_at, seconds, 1 if skipped else 0),
    )


# ---------------------------------------------------------------------------
# «Недавно слушали»


def test_home_lists_recently_heard_tracks_newest_first_once_each(client):
    play("A/a1.m4a", at(3))
    play("B/b1.m4a", at(2))
    play("A/a1.m4a", at(1))  # heard again: moves up, not listed twice
    play("C/c1.m4a", at(0), seconds=3.0, skipped=True)  # skipped at once
    play("A/a2.m4a", at(0), seconds=2.0)  # not marked skipped, but barely heard
    play("Gone/deleted.m4a", at(0))  # the file has since left the library

    heard = client.get("/api/home").json()["heard"]

    assert [t["path"] for t in heard] == ["A/a1.m4a", "B/b1.m4a"]
    assert heard[0] == {
        "path": "A/a1.m4a",
        "artist": "Аня",
        "title": "Первая",
        "album": "X",
        "duration": 200.0,
    }


def test_home_heard_is_empty_without_a_journal_and_capped(client):
    assert client.get("/api/home").json()["heard"] == []

    for i in range(30):
        play(f"Gone/{i}.m4a", at(0, minute=i))
    for i, row in enumerate(ROWS):
        play(row["path"], at(1, minute=i))

    heard = client.get("/api/home").json()["heard"]
    # 30 newer plays of deleted files do not push the real ones out.
    assert [t["path"] for t in heard] == [r["path"] for r in reversed(ROWS)]


# ---------------------------------------------------------------------------
# /api/stats


def test_stats_require_auth(client):
    client.headers.pop("Authorization")
    assert client.get("/api/stats").status_code == 401


def test_stats_reject_an_unknown_period(client):
    assert client.get("/api/stats?period=week").status_code == 422


def test_stats_empty_journal(client):
    body = client.get("/api/stats?period=month").json()

    assert body["plays"] == 0
    assert body["seconds"] == 0
    assert body["artists"] == [] and body["tracks"] == []
    assert body["chart"]["unit"] == "day"
    assert len(body["chart"]["bars"]) == 30
    assert all(b["seconds"] == 0 for b in body["chart"]["bars"])


def test_stats_totals_tops_and_chart(client):
    play("A/a1.m4a", at(0), seconds=200)
    play("A/a1.m4a", at(1), seconds=200)
    play("A/a1.m4a", at(1, hour=13), seconds=100)
    play("A/a2.m4a", at(2), seconds=180)
    play("B/b1.m4a", at(2), seconds=240)
    play("C/c1.m4a", at(0), seconds=5, skipped=True)  # a skip counts nowhere
    play("Gone/x.m4a", at(0), seconds=60)  # deleted: time counts, tops do not

    body = client.get("/api/stats?period=month").json()

    assert body["period"] == "month"
    assert body["plays"] == 6
    assert body["seconds"] == 200 + 200 + 100 + 180 + 240 + 60
    # The artist is the one the artist page is keyed by: albumartist, first of a feat.
    assert body["artists"] == [
        {"name": "Аня", "plays": 4, "seconds": 680.0, "cover": "A/a1.m4a"},
        {"name": "Борис", "plays": 1, "seconds": 240.0, "cover": "B/b1.m4a"},
    ]
    tracks = body["tracks"]
    assert [(t["path"], t["plays"]) for t in tracks] == [
        ("A/a1.m4a", 3),
        ("B/b1.m4a", 1),
        ("A/a2.m4a", 1),
    ]
    assert tracks[0]["title"] == "Первая" and tracks[0]["artist"] == "Аня"
    assert tracks[0]["seconds"] == 500.0

    bars = {b["key"]: b["seconds"] for b in body["chart"]["bars"]}
    today = datetime.date.today()
    assert bars[today.isoformat()] == 260
    assert bars[(today - datetime.timedelta(days=1)).isoformat()] == 300
    assert bars[(today - datetime.timedelta(days=2)).isoformat()] == 420
    keys = [b["key"] for b in body["chart"]["bars"]]
    assert keys == sorted(keys) and keys[-1] == today.isoformat()


def test_stats_month_starts_29_days_back_at_midnight(client):
    """Thirty days with today: the first second of the 30th day counts, the
    last second before it does not -- compared as the same local-time text."""
    play("A/a1.m4a", at(29, hour=0, minute=0, second=0), seconds=100)
    play("A/a2.m4a", at(30, hour=23, minute=59, second=59), seconds=100)

    month = client.get("/api/stats?period=month").json()
    assert month["plays"] == 1
    assert [t["path"] for t in month["tracks"]] == ["A/a1.m4a"]
    assert month["chart"]["bars"][0] == {
        "key": (datetime.date.today() - datetime.timedelta(days=29)).isoformat(),
        "seconds": 100.0,
    }

    year = client.get("/api/stats?period=year").json()
    assert year["plays"] == 2
    assert year["chart"]["unit"] == "month"
    assert len(year["chart"]["bars"]) == 12
    assert year["chart"]["bars"][-1]["key"] == datetime.date.today().strftime("%Y-%m")


def test_stats_year_and_all_time(client):
    today = datetime.date.today()
    first_of_month = today.replace(day=1)
    # The first day of the month eleven months back opens the year.
    start = first_of_month
    for _ in range(11):
        start = (start - datetime.timedelta(days=1)).replace(day=1)
    inside = f"{start.isoformat()} 00:00:00"
    before = f"{(start - datetime.timedelta(days=1)).isoformat()} 23:59:59"
    long_ago = "2020-01-15 10:00:00"
    play("A/a1.m4a", inside, seconds=60)
    play("A/a1.m4a", before, seconds=60)
    play("C/c1.m4a", long_ago, seconds=60)

    year = client.get("/api/stats?period=year").json()
    assert year["plays"] == 1
    assert year["chart"]["bars"][0] == {"key": start.strftime("%Y-%m"), "seconds": 60.0}

    every = client.get("/api/stats?period=all").json()
    assert every["plays"] == 3
    assert every["chart"]["unit"] == "month"
    keys = [b["key"] for b in every["chart"]["bars"]]
    # From the first month with a play to this one, gaps included.
    assert keys[0] == "2020-01" and keys[-1] == today.strftime("%Y-%m")
    assert len(keys) == len(set(keys))
    assert every["artists"][0] == {"name": "Аня", "plays": 2, "seconds": 120.0, "cover": "A/a1.m4a"}


def test_stats_top_lists_stop_at_ten(client, monkeypatch):
    rows = [
        {"path": f"P/{i}.m4a", "artist": f"Артист {i}", "title": f"Трек {i}", "duration": 100.0}
        for i in range(15)
    ]
    monkeypatch.setattr(library, "library_index", lambda: rows)
    for i, row in enumerate(rows):
        for _ in range(i + 1):
            play(row["path"], at(0), seconds=50)

    body = client.get("/api/stats?period=all").json()

    assert len(body["tracks"]) == 10 and len(body["artists"]) == 10
    assert body["tracks"][0]["path"] == "P/14.m4a" and body["tracks"][0]["plays"] == 15
    assert body["plays"] == sum(range(1, 16))
