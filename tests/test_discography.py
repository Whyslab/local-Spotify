"""«Артист целиком» (02.10.2026): дискография, версии, ожидание поиска.

Сеть подменяется: Deezer — `similar._get` и `discography._about`, YouTube —
`outside._search`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from adder import config, db, discography, outside, runtime, similar


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
    while not runtime.TASK_QUEUE.empty():
        runtime.TASK_QUEUE.get_nowait()
    runtime.PROCESSING_URLS.clear()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-secret"}


# --- версии -----------------------------------------------------------------


@pytest.mark.parametrize(
    "title, version",
    [
        ("Live Forever", False),
        ("Alive", False),
        ("Song (Album Version)", False),
        ("Мама (prod. by Live)", False),
        ("Song (feat. Live)", False),
        ("Slowed Down Love", False),
        ("Мой Дилер - Инопланетянин", False),
        ("Вслух (Скит)", False),
        ("3:55 (Freestyle)", False),
        ("Song (Live Version)", True),
        ("Кукла (Sped Up)", True),
        ("Кукла sped up", True),
        ("Love (slowed + reverb)", True),
        ("X - Instrumental", True),
        ("Валиум (Reworked)", True),
        ("Песня [Remix]", True),
        ("Песня (Ремикс)", True),
        ("Song (Acoustic)", True),
        ("VK Акустика", True),
        ("Стиль (Другая версия)", True),
        ("Завидуют (Версия 2)", True),
        ("Beats (Instrumentals)", True),
        ("Дотла (DZA Reflip)", True),
        ("Альбом (Deluxe Version)", False),
        ("Песня (Remastered Version)", False),
        ("Плюс - Минус", False),
        ("Песня (минус)", True),
    ],
)
def test_versions_are_told_from_originals(title, version):
    assert discography.is_version(title) is version


# --- дискография --------------------------------------------------------------


def _release(rid, title, kind, date, tracks=1):
    return {
        "id": rid,
        "title": title,
        "record_type": kind,
        "release_date": date,
        "nb_tracks": tracks,
        "cover_medium": "",
    }


@pytest.fixture
def deezer(monkeypatch):
    releases = [
        _release(3, "Песня", "single", "2020-01-01"),  # потом вышла на альбоме
        _release(4, "Песня (Sped Up)", "single", "2020-02-01"),
        _release(1, "Альбом", "album", "2021-01-01", 3),
        _release(5, "Лучшее", "compile", "2022-01-01", 2),
        _release(2, "EP", "ep", "2019-01-01", 1),
    ]
    tracks = {
        1: ["Песня", "Старая", "Песня (Remix)"],
        2: ["Ранняя"],
        3: ["Песня"],
        4: ["Песня (Sped Up)"],
        5: ["Песня", "Старая"],
    }

    def get(client, url, params):
        if url.endswith("/albums"):
            return releases
        rid = int(url.split("/album/")[1].split("/")[0])
        return [
            {"title": t, "artist": {"name": "Артист"}, "duration": 100 + n}
            for n, t in enumerate(tracks[rid])
        ]

    monkeypatch.setattr(similar, "_get", get)
    monkeypatch.setattr(discography, "_about", lambda client, aid: {"id": aid, "name": "Артист"})


def test_discography_lists_originals_once_and_marks_what_is_there(deezer):
    library = [{"artist": "Артист", "title": "Старая"}]
    data = discography.discography("7", library)

    assert [(r["title"], r["kind"]) for r in data["releases"]] == [
        ("Альбом", "album"),
        ("EP", "ep"),
    ]
    album = data["releases"][0]["tracks"]
    assert [(t["title"], t["have"]) for t in album] == [("Песня", False), ("Старая", True)]
    # Сингл «Песня» (уже на альбоме) и ремикс — скрыты; sped up-сингл —
    # скрытый релиз; сборник не берётся вовсе.
    assert (data["hidden"], data["hidden_releases"]) == (2, 1)
    assert all(r["kind"] != "compile" for r in data["releases"])


def test_have_is_recognised_across_scripts_and_features(deezer, monkeypatch):
    monkeypatch.setattr(discography, "_about", lambda client, aid: {"id": aid, "name": "Vozrast"})
    library = [
        {"artist": "Возраст", "title": "Песня"},  # кириллицей
        {"artist": "Друг • Vozrast", "title": "Старая"},  # он здесь гость
        {"artist": "Чужой", "title": "Ранняя"},  # то же название, другой артист
    ]
    data = discography.discography("7", library)
    have = {t["title"]: t["have"] for r in data["releases"] for t in r["tracks"]}
    assert have == {"Песня": True, "Старая": True, "Ранняя": False}


def test_same_title_different_length_is_another_song(monkeypatch):
    """«Биг бой» на 260 с и «Биг бой» на 70 с — разные треки (Платина)."""
    releases = [_release(1, "Сладких снов", "album", "2021"), _release(2, "РНБ", "album", "2020")]
    lengths = {1: 260, 2: 70}
    monkeypatch.setattr(
        similar,
        "_get",
        lambda client, url, params: (
            releases
            if url.endswith("/albums")
            else [
                {
                    "title": "Биг бой",
                    "artist": {"name": "Платина"},
                    "duration": lengths[int(url.split("/album/")[1].split("/")[0])],
                }
            ]
        ),
    )
    monkeypatch.setattr(discography, "_about", lambda client, aid: {"id": aid, "name": "Платина"})
    data = discography.discography("7", [{"artist": "Платина", "title": "Биг бой", "duration": 70}])
    tracks = [(t["duration"], t["have"]) for r in data["releases"] for t in r["tracks"]]
    assert sorted(tracks) == [(70, True), (260, False)]


def test_have_matches_whole_names_not_pieces(deezer, monkeypatch):
    """«DK» не находится внутри «madk1d»."""
    monkeypatch.setattr(discography, "_about", lambda client, aid: {"id": aid, "name": "DK"})
    monkeypatch.setattr(
        similar,
        "_get",
        lambda client, url, params: (
            [_release(1, "A", "album", "2020")]
            if url.endswith("/albums")
            else [{"title": "Ночь", "artist": {"name": "DK"}, "duration": 100}]
        ),
    )
    library = [{"artist": "madk1d", "title": "Ночь", "duration": 100}]
    data = discography.discography("7", library)
    assert data["releases"][0]["tracks"][0]["have"] is False


def test_find_artist_skips_an_empty_exact_entry(monkeypatch):
    """«Алина Орлова» кириллицей у Deezer — пустышка; её песни у «Alina Orlova»."""
    found = [
        {"id": "1", "name": "Alina Orlova", "picture": "", "fans": 12000, "albums": 20},
        {"id": "2", "name": "Алина Орлова", "picture": "", "fans": 300, "albums": 0},
        {"id": "3", "name": "Борис", "picture": "", "fans": 40, "albums": 2},
        {"id": "4", "name": "Boris", "picture": "", "fans": 9000, "albums": 9},
    ]
    monkeypatch.setattr(discography, "search_artists", lambda q, limit=8: found)
    assert discography.find_artist("Алина Орлова")["id"] == "1"
    assert discography.find_artist("Борис")["id"] == "3"  # точное с релизами — главнее


# --- ожидание поиска -----------------------------------------------------------


def _track(title, duration=100):
    return {"artist": "Артист", "title": title, "duration": duration, "album": "1"}


def test_add_skips_what_already_waits_and_survives_a_restart():
    assert discography.add([_track("Раз"), _track("Два")]) == 2
    assert discography.add([_track("Раз"), {"artist": "", "title": "Без артиста"}]) == 0
    # «Перезапуск» — новый процесс читает то же с диска.
    assert [i["title"] for i in discography._load()] == ["Раз", "Два"]
    assert discography.status()["wait"] == 2


def _drain(queue_one):
    while discography.step(queue_one) == 0:
        pass


def test_step_marks_each_track(monkeypatch):
    discography.add([_track("Найдётся"), _track("Нет такой")])
    answers = iter(["queued", "missed"])
    _drain(lambda item: next(answers))

    st = discography.status()
    assert (st["wait"], st["queued"], st["missed"]) == (0, 1, 1)
    assert st["missed_tracks"] == [{"artist": "Артист", "title": "Нет такой"}]


def test_a_failing_search_waits_without_holding_the_rest(monkeypatch):
    discography.add([_track("Не ищется"), _track("Ищется")])

    def queue_one(item):
        if item["title"] == "Не ищется":
            raise discography.TryLater("HTTP Error 429")
        return "queued"

    pause = discography.step(queue_one)
    assert pause == 0
    _drain(queue_one)
    st = discography.status()
    assert (st["wait"], st["queued"], st["failing"]) == (1, 1, 1)
    assert "429" in st["last_error"]
    # Все ждущие отложены — шаг говорит, сколько спать, а не крутится.
    assert discography.step(queue_one) > 60


def test_a_search_that_never_works_gives_up(monkeypatch):
    monkeypatch.setattr(discography, "RETRY_AFTER", 0)
    discography.add([_track("Никак")])

    def queue_one(item):
        raise discography.TryLater("yt-dlp not found")

    for _ in range(discography.MAX_ATTEMPTS + 2):
        discography.step(queue_one)
    st = discography.status()
    assert (st["wait"], st["missed"]) == (0, 1)


def test_a_crash_on_one_track_does_not_stop_the_rest():
    discography.add([_track("Сломает"), _track("Следующий")])

    def queue_one(item):
        if item["title"] == "Сломает":
            raise ValueError("boom")
        return "queued"

    _drain(queue_one)
    assert discography.status()["missed"] == 1
    assert discography.status()["queued"] == 1


def test_clear_finished_keeps_only_waiting_and_cancel_drops_it():
    discography.add([_track("Готов"), _track("Ждёт")])
    discography.step(lambda item: "queued")
    discography.clear_finished()
    assert [i["title"] for i in discography._load()] == ["Ждёт"]
    assert discography.cancel_waiting() == 1
    assert discography.status()["total"] == 0


def test_a_broken_list_is_set_aside_not_overwritten():
    runtime.ARTIST_IMPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    runtime.ARTIST_IMPORT_FILE.write_text("{не json", encoding="utf-8")
    discography.add([_track("Новый")])
    aside = list(runtime.ARTIST_IMPORT_FILE.parent.glob("artist-import.json.broken-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "{не json"
    assert [i["title"] for i in discography._load()] == ["Новый"]


# --- поиск на YouTube и постановка в загрузку -----------------------------------


def _entry(vid, title, channel, duration):
    return {"id": vid, "title": title, "channel": channel, "duration": duration}


def test_the_studio_recording_is_queued_with_its_album(client, monkeypatch):
    from adder import app as app_module

    monkeypatch.setattr(
        outside,
        "_search",
        lambda q: [
            _entry("live1", "Артист - Песня (Live)", "Фан", 200),
            _entry("good1", "Песня", "Артист - Topic", 101),
        ],
    )
    item = _track("Песня", 100)
    item["album"] = "555"

    assert app_module._queue_artist_track(item) == "queued"
    task = db.db_query("SELECT url, album_hint FROM tasks ORDER BY id DESC LIMIT 1")[0]
    assert task["url"].endswith("good1")
    assert task["album_hint"] == "555"
    # Второй раз та же запись уже в работе — не дубль.
    assert app_module._queue_artist_track(item) == "had"


def test_no_same_recording_is_missed_and_a_failed_search_waits(client, monkeypatch):
    from adder import app as app_module

    monkeypatch.setattr(outside, "_search", lambda q: [_entry("x", "Песня", "Кто-то", 300)])
    assert app_module._queue_artist_track(_track("Песня", 100)) == "missed"

    def broken(q):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(outside, "_search", broken)
    with pytest.raises(discography.TryLater):
        app_module._queue_artist_track(_track("Песня", 100))
    # После 429 тормозят все вызовы yt-dlp, как очередь загрузок.
    assert runtime.youtube_pause_left() > 0


# --- адреса -------------------------------------------------------------------


def test_artist_routes_require_a_token(client):
    assert client.get("/api/artists/search", params={"q": "x"}).status_code == 401
    assert client.get("/api/artists/find", params={"name": "x"}).status_code == 401
    assert client.get("/api/artists/1/discography").status_code == 401
    assert client.post("/api/artists/import", json={"tracks": []}).status_code == 401
    assert client.get("/api/artists/import").status_code == 401
    assert client.delete("/api/artists/import").status_code == 401


def test_a_silent_deezer_is_a_502_not_an_empty_list(client, auth, monkeypatch):
    def down(client, url, params):
        raise similar._Unreachable("ответ 503")

    monkeypatch.setattr(similar, "_get", down)
    assert client.get("/api/artists/search", params={"q": "x"}, headers=auth).status_code == 502


def test_discography_wants_a_deezer_number(client, auth):
    assert client.get("/api/artists/abc/discography", headers=auth).status_code == 400
    assert client.get("/api/artists/²/discography", headers=auth).status_code == 400


@pytest.mark.parametrize(
    "bad",
    [{"duration": float("nan")}, {"duration": -1}, {"album": "1/../x"}],
    ids=["nan", "negative", "album-path"],
)
def test_import_refuses_values_that_would_skip_checks(client, auth, bad):
    body = json.dumps({"tracks": [{**_track("Раз"), **bad}]})  # NaN пишется как NaN
    response = client.post(
        "/api/artists/import",
        content=body,
        headers={**auth, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert discography.status()["total"] == 0


def test_import_puts_tracks_in_waiting(client, auth):
    response = client.post(
        "/api/artists/import",
        json={"tracks": [_track("Раз"), _track("Два")]},
        headers=auth,
    )
    assert response.status_code == 200
    assert response.json()["added"] == 2
    assert client.get("/api/artists/import", headers=auth).json()["wait"] == 2


def test_cancel_drops_what_still_waits(client, auth):
    client.post("/api/artists/import", json={"tracks": [_track("Раз")]}, headers=auth)
    st = client.delete("/api/artists/import", params={"waiting": "true"}, headers=auth).json()
    assert (st["wait"], st["total"]) == (0, 0)


def test_a_longer_clip_version_in_the_library_still_counts(monkeypatch):
    """В фонотеке «Сахарный человек» из клипа — 302 с, у Deezer 165 с.
    Это та же песня: качать её второй раз незачем."""
    monkeypatch.setattr(
        similar,
        "_get",
        lambda client, url, params: (
            [_release(1, "A", "album", "2020")]
            if url.endswith("/albums")
            else [{"title": "САХАРНЫЙ ЧЕЛОВЕК", "artist": {"name": "GONE.Fludd"}, "duration": 165}]
        ),
    )
    monkeypatch.setattr(
        discography, "_about", lambda client, aid: {"id": aid, "name": "GONE.Fludd"}
    )
    library = [{"artist": "GONE.Fludd", "title": "САХАРНЫЙ ЧЕЛОВЕК", "duration": 302}]
    data = discography.discography("7", library)
    assert data["releases"][0]["tracks"][0]["have"] is True
