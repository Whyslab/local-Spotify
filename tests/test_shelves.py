"""Полки главной, выгруженные в файлы: что на телефоне окажется ровно то же.

Фонотека здесь временная, и наружу тесты не ходят: `discover` спрашивает у
Deezer похожих артистов, поэтому она подменяется. Настоящая фонотека и
настоящая сеть в тестах означали бы, что прогон зависит от того, что сегодня
ответил Deezer и что лежит в ~/Music.
"""

import sqlite3

import pytest

from adder import config, db, library, playlists, shelves

# Сорока измеренных треков хватает, чтобы у moods.collections появились все
# четыре полки: границы там — четверти самой фонотеки, а каждая полка
# показывается только от восьми треков. Темп, энергия и яркость растут вместе,
# поэтому «В машину» (быстрее и громче половины) непуст.
TOTAL = 40


def _features() -> list[dict]:
    return [
        {
            "path": f"Artist {i:02d}/Singles/Track {i:02d}.m4a",
            "tempo": 80.0 + i,
            "energy": 0.10 + i * 0.01,
            "brightness": 1000.0 + i * 50,
        }
        for i in range(TOTAL)
    ]


def _index_rows() -> list[dict]:
    return [
        {
            "path": row["path"],
            "artist": f"Artist {i:02d}",
            "title": f"Track {i:02d}",
            "album": "",
            "duration": 180.0,
            "albumartist": "",
            "track": None,
            "haystack": "",
        }
        for i, row in enumerate(_features())
    ]


def _discover_returning(count: int):
    """Подмена `discover`: та же форма ответа, но без похода в сеть."""
    paths = [row["path"] for row in _features()[-count:]] if count else []
    return lambda rows, **kwargs: {"based_on": [], "tracks": [{"path": p} for p in paths]}


@pytest.fixture()
def shelf_library(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    config.LIBRARY.mkdir(parents=True)

    rows = _index_rows()
    monkeypatch.setattr(library, "library_index", lambda: rows)
    monkeypatch.setattr(db, "db_query", lambda sql, params=(): _features())
    monkeypatch.setattr(shelves, "discover", _discover_returning(10))
    return config.LIBRARY


ALL_SHELVES = {
    "★ В машину",
    "★ Разогнаться",
    "★ На работу",
    "★ Поярче",
    "★ Может понравиться",
}


def test_every_shelf_becomes_a_playlist_with_the_star(shelf_library):
    """Приставка — то, по чему полку видно в Amperfy и отличают от своей подборки."""
    report = shelves.export()

    assert {item["name"] for item in report} == ALL_SHELVES
    assert all(item["written"] for item in report)
    assert {path.name for path in shelf_library.glob("*.m3u")} == {
        f"{name}.m3u" for name in ALL_SHELVES
    }


def test_written_file_is_an_m3u_of_relative_paths(shelf_library):
    """Navidrome читает пути от корня фонотеки; абсолютный путь он не найдёт."""
    report = {item["name"]: item for item in shelves.export()}
    text = (shelf_library / "★ Разогнаться.m3u").read_text(encoding="utf-8")

    assert text.startswith("#EXTM3U")
    paths = playlists.parse(text)
    # Число строк — то же, что скрипт назовёт в отчёте: иначе отчёт врёт.
    assert len(paths) == report["★ Разогнаться"]["count"]
    assert all(not path.startswith("/") for path in paths)


def test_a_short_shelf_is_skipped_and_leaves_yesterdays_file_alone(shelf_library, monkeypatch):
    """Вчерашняя подборка лучше пустой: полка из трёх треков файл не трогает."""
    monkeypatch.setattr(shelves, "discover", _discover_returning(3))
    name = f"{shelves.PREFIX}{shelves.DISCOVER_NAME}"
    playlists.create(name, ["Artist 00/Singles/Track 00.m4a"])
    before = playlists.playlist_path(name).read_text(encoding="utf-8")

    report = {item["name"]: item for item in shelves.export()}

    assert report[name]["written"] is False
    assert report[name]["count"] == 3
    assert playlists.playlist_path(name).read_text(encoding="utf-8") == before
    # И пропуск не должен выглядеть как перезапись: копии в истории нет.
    assert not (playlists.HISTORY_DIR / name).exists()


def test_a_second_run_overwrites_a_hand_edit(shelf_library):
    """Обещание из README: правка внутри `★ ...` не живёт, но и не теряется."""
    shelves.export()
    path = shelf_library / "★ На работу.m3u"
    original = path.read_text(encoding="utf-8")
    path.write_text("#EXTM3U\nПравка руками.m4a\n", encoding="utf-8")

    shelves.export()

    assert path.read_text(encoding="utf-8") == original
    archived = list((playlists.HISTORY_DIR / "★ На работу").glob("*.m3u"))
    assert any("Правка руками.m4a" in copy.read_text(encoding="utf-8") for copy in archived)


def test_a_measured_track_that_left_the_library_is_not_written(shelf_library, monkeypatch):
    """Строка в audio_features живёт дольше файла, а битая строка в подборке — нет."""
    ghost = "Удалённый/Singles/Нет его.m4a"
    measured = _features() + [
        # Самый быстрый, громкий и звонкий: без отбора по индексу он попал бы
        # сразу в три полки.
        {"path": ghost, "tempo": 200.0, "energy": 0.99, "brightness": 9000.0}
    ]
    monkeypatch.setattr(db, "db_query", lambda sql, params=(): measured)

    shelves.export()

    for file in shelf_library.glob("*.m3u"):
        assert ghost not in playlists.parse(file.read_text(encoding="utf-8"))


def test_shelves_change_from_day_to_day_and_hold_within_a_day():
    """Полки перемешивались зерном «число треков» — и каждый день были одни и
    те же, пока не менялась фонотека. Теперь зерно — сутки полок."""
    import datetime

    from adder import moods

    rows = [
        {"path": f"a/{i}.m4a", "tempo": 60 + i, "energy": 0.1 + i / 400, "brightness": 1000 + i}
        for i in range(200)
    ]

    def car(seed):
        return next(m.paths for m in moods.collections(rows, limit=10, seed=seed) if m.key == "car")

    day = datetime.datetime(2026, 10, 1, 12, 0)
    today = shelves.shelf_seed(day)
    assert shelves.shelf_seed(day.replace(hour=5)) == today  # утро — те же сутки
    assert shelves.shelf_seed(day.replace(hour=3)) == today - 1  # до 04:00 — ещё вчерашние
    assert car(today) == car(today)
    assert car(today) != car(today + 1)
    # Полки одного дня перемешаны каждая по-своему, а не одним порядком.
    one = {m.key: m.paths for m in moods.collections(rows, limit=200, seed=today)}
    assert one["car"][:5] != [p for p in one["run"] if p in one["car"]][:5]


def test_home_answers_over_http_with_moods_discover_and_albums(shelf_library, monkeypatch):
    """The home page's one request, end to end through the route (it had no test)."""
    from fastapi.testclient import TestClient

    from adder import app as app_module
    from adder import navidrome

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    client = TestClient(app_module.app)

    assert client.get("/api/home").status_code == 401
    response = client.get("/api/home", headers={"Authorization": "Bearer test-secret"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"moods", "discover", "albums", "releases", "weekly"}
    assert body["moods"] and all({"key", "name", "tracks"} <= set(m) for m in body["moods"])
    assert len(body["discover"]["tracks"]) == 10


def test_home_has_new_releases(shelf_library, monkeypatch):
    """Plan 4c.2: the night's finds reach the home page as they are, with no
    request to Deezer of its own."""
    import json

    from fastapi.testclient import TestClient

    from adder import app as app_module
    from adder import navidrome, runtime, similar

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    monkeypatch.setattr(similar, "_get", lambda *a, **k: pytest.fail("home asked Deezer"))
    found = {
        "id": "11",
        "artist": "Seed",
        "title": "Fresh Album",
        "kind": "album",
        "date": "2026-10-04",
        "cover": "https://e-cdns-images.dzcdn.net/images/cover/x/250x250.jpg",
        "seen": 1.0,
        "queued": True,
    }
    runtime.NEW_RELEASES_FILE.write_text(
        json.dumps({"artists": {}, "releases": [found], "checked_at": 1.0}), encoding="utf-8"
    )
    client = TestClient(app_module.app)
    body = client.get("/api/home", headers={"Authorization": "Bearer test-secret"}).json()

    assert body["releases"] == [
        {k: found[k] for k in ("id", "artist", "title", "kind", "date", "cover")}
    ]


def test_maybe_like_follows_listening(tmp_path, monkeypatch):
    """Plan 4c.4: «Может понравиться» starts from what is being listened to,
    and only falls back to the biggest artists of the library."""
    from adder import runtime, similar

    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "shelves.db")
    db.db_init()
    neighbours = {"heard": ["Kin of Heard"], "big": ["Cousin of Big"]}
    monkeypatch.setattr(
        similar, "similar_artists", lambda name, cache, limit=10: neighbours.get(name.lower(), [])
    )

    def row(artist, n):
        return {"path": f"{artist}/{n}.m4a", "artist": artist, "title": f"{artist} {n}"}

    rows = [row("Big", i) for i in range(5)] + [row("Heard", 0)]
    rows += [row("Kin of Heard", i) for i in range(3)] + [row("Cousin of Big", i) for i in range(3)]

    # No journal yet: the most collected artist leads.
    assert shelves.discover(rows, top=1)["based_on"] == ["Big"]

    def play(path, skipped=0, days_ago=1):
        db.db_exec(
            "INSERT INTO plays(path, played_at, skipped) "
            "VALUES(?, datetime('now', 'localtime', ?), ?)",
            (path, f"-{days_ago} days", skipped),
        )

    for _ in range(3):
        play("Heard/0.m4a")
    play("Big/0.m4a", skipped=1)  # a skip is not listening
    play("Big/1.m4a", days_ago=60)  # too long ago
    found = shelves.discover(rows, top=1)
    assert found["based_on"] == ["Heard"]
    assert {t["artist"] for t in found["tracks"]} == {"Kin of Heard"}
    # The fallback still fills the rest, without repeating anyone.
    assert shelves.favourite_artists(rows, top=3)[:2] == ["Heard", "Big"]


def test_favourites_merge_letter_case_and_fall_back_like_new_releases(tmp_path, monkeypatch):
    """Review of 4c.4: «LIZER» and «Lizer» are one artist, not two of six
    places; the fallback prefers the album artist, as new releases do."""
    from adder import runtime

    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "shelves.db")
    db.db_init()

    def row(artist, n, albumartist=""):
        return {
            "path": f"{artist}/{n}.m4a",
            "artist": artist,
            "albumartist": albumartist,
            "title": f"{artist} {n}",
        }

    rows = [row("LIZER", 0), row("Lizer", 1), row("lizer", 2)]
    rows += [row("Big", i) for i in range(2)]
    rows += [row(f"Guest {i}", i, albumartist="Compiler") for i in range(4)]
    for path in ("LIZER/0.m4a", "Lizer/1.m4a"):
        db.db_exec(
            "INSERT INTO plays(path, played_at, skipped) "
            "VALUES(?, datetime('now', 'localtime', '-1 days'), 0)",
            (path,),
        )

    names = shelves.favourite_artists(rows, top=3)
    assert names[0] == "LIZER"
    assert [n.lower() for n in names].count("lizer") == 1
    # Four tracks of one album artist outweigh two of Big.
    assert names[1:] == ["Compiler", "Big"]


def test_favourites_without_a_readable_journal_come_from_the_library(monkeypatch):
    def broken(sql, params=()):
        raise sqlite3.OperationalError("no such table: plays")

    monkeypatch.setattr(db, "db_query", broken)
    rows = [{"path": f"Big/{i}.m4a", "artist": "Big"} for i in range(3)]
    rows += [{"path": "Small/0.m4a", "artist": "Small"}]
    assert shelves.favourite_artists(rows, top=2) == ["Big", "Small"]
