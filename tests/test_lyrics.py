"""Тексты песен: поиск, когда точный запрос промахнулся, и обход фонотеки.

В сеть тесты не ходят: conftest глушит `_ask` и `_search`, а здесь они
подменяются тем, что нужно конкретному тесту.
"""

import json
import threading
import time

import pytest

from adder import library, lyrics

REAL_SEARCH = lyrics._search


def entry(artist, track, duration, synced="[00:01.00] строка", plain="строка", **extra):
    return {
        "artistName": artist,
        "trackName": track,
        "duration": duration,
        "syncedLyrics": synced,
        "plainLyrics": plain,
        "instrumental": False,
        **extra,
    }


# ---------------------------------------------------------------------------
# Как ещё может называться трек
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Why's this dealer? (Lyrics)", "Why's this dealer?"),
        ("SICKO MODE (Audio)", "SICKO MODE"),
        ("Abu Dhabi Ba6y (feat. OG Buda & MAYOT)", "Abu Dhabi Ba6y"),
        ("GONE.Fludd — ХУДРИЧ [prod. by CAKEBOY]", "GONE.Fludd — ХУДРИЧ"),
        ("Универ - Speed up", "Универ - Speed up"),
    ],
)
def test_youtube_tails_are_dropped(title, expected):
    assert lyrics.title_variants(title)[0] == expected


def test_an_artist_glued_into_the_title_gives_a_second_variant():
    assert lyrics.title_variants("GONE.Fludd, ЛСП — Ути-Пути", "GONE.Fludd") == [
        "GONE.Fludd, ЛСП — Ути-Пути",
        "Ути-Пути",
    ]


@pytest.mark.parametrize("title", ["Козловский - Remix", "worry - Slowed", "Пластинки - Bonus"])
def test_a_dash_that_is_not_after_the_artist_gives_no_one_word_title(title):
    """«Remix» одним словом совпал бы с любым ремиксом того же артиста."""
    assert lyrics.title_variants(title, "PHARAOH") == [title]


def test_with_in_brackets_is_part_of_the_title():
    assert lyrics.title_variants("Love (With You)")[0] == "Love (With You)"


def test_every_artist_of_a_featuring_line_counts():
    assert lyrics.artist_names("Элджей • FEDUK") == ["Элджей", "FEDUK"]
    assert lyrics.artist_names("SLAVA MARLOW & MORGENSHTERN") == ["SLAVA MARLOW", "MORGENSHTERN"]


# ---------------------------------------------------------------------------
# Выбор из выдачи поиска
# ---------------------------------------------------------------------------


def test_the_same_song_is_found_under_a_reordered_or_latin_artist_name():
    assert lyrics._pick([entry("Cute Baby", "tab on me", 150)], "Baby Cute", "tab on me", 151)
    assert lyrics._pick(
        [entry("Gio Pika", "Фонтанчик с дельфином", 200)], "Гио Пика", "Фонтанчик с дельфином", 199
    )


def test_a_song_of_the_same_name_by_someone_else_is_refused():
    """Поиск по одному названию находил «Break Up» у U-KISS вместо Baby Cute."""
    assert lyrics._pick([entry("U-KISS", "Break up", 180)], "Baby Cute", "break UP", 180) is None


def test_another_version_of_another_length_is_refused():
    """Тайминги от другой версии побегут мимо музыки."""
    assert lyrics._pick([entry("MAYOT", "4:30", 150)], "MAYOT", "4:30", 190) is None


def test_a_record_without_any_text_is_skipped():
    empty = entry("MAYOT", "4:30", 150, synced="", plain="")
    good = entry("MAYOT", "4:30", 151)
    assert lyrics._pick([empty, good], "MAYOT", "4:30", 150) is good


def test_a_catalogue_record_without_a_title_matches_nothing():
    for title in ("", "(Official Video)", "?"):
        assert lyrics._pick([entry("MAYOT", title, 150)], "MAYOT", "4:30", 150) is None


def test_a_short_foreign_title_does_not_stand_for_a_long_one():
    assert lyrics._pick([entry("Travis Scott", "Mode", 312)], "Travis Scott", "SICKO MODE", 312)
    assert (
        lyrics._pick(
            [entry("Travis Scott", "Mode", 312)], "Travis Scott", "Sicko Mode Part Two", 312
        )
        is None
    )


def test_an_instrumental_upload_does_not_hide_the_real_text():
    minus = entry("Drake", "God's Plan (Instrumental)", 199, synced="", plain="", instrumental=True)
    real = entry("Drake", "God's Plan", 199)
    assert lyrics._pick([minus, real], "Drake", "God's Plan", 199) is real


def test_ukrainian_letters_are_transliterated():
    assert lyrics._words("Стефанія") == lyrics._words("Stefaniya")


def test_a_featured_artist_is_enough_to_match():
    found = lyrics._pick(
        [entry("FEDUK", "Розовое вино", 200)], "Элджей • FEDUK", "Розовое вино", 201
    )
    assert found is not None


# ---------------------------------------------------------------------------
# for_track: точный запрос, потом поиск
# ---------------------------------------------------------------------------


ROW = {
    "path": "A/B/t.m4a",
    "artist": "Travis Scott",
    "title": "SICKO MODE (Audio)",
    "album": "",
    "duration": 312,
}


def test_a_miss_of_the_exact_request_falls_back_to_search(monkeypatch):
    monkeypatch.setattr(lyrics, "_ask", lambda *a: {})
    monkeypatch.setattr(
        lyrics, "_search", lambda artist, title, duration: entry("Travis Scott", "SICKO MODE", 312)
    )

    result = lyrics.for_track(ROW["path"], row=ROW)
    assert result["found"] is True
    assert result["synced"][0]["line"] == "строка"


def test_an_instrumental_is_named_and_not_searched_further(monkeypatch):
    monkeypatch.setattr(
        lyrics, "_ask", lambda *a: entry("X", "Y", 100, synced="", plain="", instrumental=True)
    )

    def search(*a):
        raise AssertionError("инструментал искать незачем")

    monkeypatch.setattr(lyrics, "_search", search)
    result = lyrics.for_track(ROW["path"], row=ROW)
    assert result["found"] is False
    assert result["instrumental"] is True
    assert "Инструментал" in result["reason"]


def test_a_silent_catalogue_is_not_remembered(monkeypatch):
    monkeypatch.setattr(lyrics, "_ask", lambda *a: None)
    result = lyrics.for_track(ROW["path"], row=ROW)
    assert result["reason"] == "Каталог текстов не ответил"
    assert lyrics.needs_lookup(ROW["path"])


def test_a_miss_found_by_the_old_rules_is_asked_again_at_once(monkeypatch):
    lyrics.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lyrics._key(ROW["path"]).write_text(
        json.dumps({"found": False, "at": time.time()}), encoding="utf-8"
    )
    assert lyrics.needs_lookup(ROW["path"])

    monkeypatch.setattr(lyrics, "_search", lambda *a: entry("Travis Scott", "SICKO MODE", 312))
    assert lyrics.for_track(ROW["path"], row=ROW)["found"] is True


def test_a_fresh_miss_by_the_current_rules_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(lyrics, "_search", lambda *a: {})
    lyrics.for_track(ROW["path"], row=ROW)
    assert not lyrics.needs_lookup(ROW["path"])
    assert lyrics.needs_lookup(ROW["path"], now=time.time() + lyrics.MISS_TTL + 1)


# ---------------------------------------------------------------------------
# Обход фонотеки
# ---------------------------------------------------------------------------


def rows(n):
    return [
        {"path": f"A/t{i}.m4a", "artist": "A", "title": f"T{i}", "album": "", "duration": 100}
        for i in range(n)
    ]


def test_backfill_asks_only_what_has_no_fresh_answer(monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: rows(4))
    asked = []

    def ask(artist, title, album, duration):
        asked.append(title)
        return entry("A", title, 100)

    monkeypatch.setattr(lyrics, "_ask", ask)
    first = lyrics.backfill(pause=0)
    assert first == {
        "asked": 4,
        "found": 4,
        "missed": 0,
        "unreachable": 0,
        "errors": 0,
        "complete": True,
    }

    second = lyrics.backfill(pause=0)
    assert second["asked"] == 0
    assert len(asked) == 4


def test_backfill_waits_out_a_refusal_and_goes_on(monkeypatch):
    """Отказ подряд — пауза, а не конец обхода; удачный ответ сбрасывает счёт."""
    monkeypatch.setattr(library, "library_index", lambda: rows(20))
    answers = iter([None, None, None] + [entry("A", "x", 100)] * 17)
    monkeypatch.setattr(lyrics, "_ask", lambda *a: next(answers))
    monkeypatch.setattr(lyrics, "last_retry_after", None)
    waited = []
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda seconds: waited.append(seconds) or False)

    stats = lyrics.backfill(stop=stop, pause=0, backoff=(7, 11, 13))
    # С отказами обход не полный — отказанные спросят через час, а не завтра.
    assert stats["complete"] is False
    assert stats["unreachable"] == 3
    assert stats["found"] == 17
    assert waited[:3] == [7, 11, 13]


def test_backfill_puts_itself_off_after_too_many_refusals_in_a_row(monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: rows(50))
    monkeypatch.setattr(lyrics, "_ask", lambda *a: None)
    stats = lyrics.backfill(pause=0, backoff=(0,))
    assert stats["unreachable"] == lyrics.BACKFILL_GIVE_UP
    assert stats["complete"] is False


def test_one_strange_answer_does_not_stop_the_backfill(monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: rows(3))
    answers = iter(["не словарь", entry("A", "x", 100), entry("A", "x", 100)])
    monkeypatch.setattr(lyrics, "_ask", lambda *a: next(answers))
    stats = lyrics.backfill(pause=0)
    assert stats["errors"] == 1
    assert stats["found"] == 2
    assert stats["complete"] is True


def test_backfill_stops_when_the_service_stops(monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: rows(50))
    monkeypatch.setattr(lyrics, "_ask", lambda *a: entry("A", "x", 100))
    stop = threading.Event()
    stop.set()
    assert lyrics.backfill(stop=stop, pause=0)["asked"] == 0


# ---------------------------------------------------------------------------
# Поиск по каталогу — с подменённым запросом
# ---------------------------------------------------------------------------


class Answer:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.headers = {}

    def json(self):
        return self._body


def test_search_tries_the_tail_after_the_artist_and_says_how_it_found(monkeypatch):
    monkeypatch.setattr(lyrics, "_search", REAL_SEARCH)
    asked = []

    def get(url, params=None):
        asked.append(params)
        if params.get("track_name") == "Ути-Пути":
            return Answer(200, [entry("GONE.Fludd", "Ути-Пути", 180)])
        return Answer(200, [])

    monkeypatch.setattr(lyrics, "_get", get)
    found = lyrics._search("GONE.Fludd", "GONE.Fludd, ЛСП — Ути-Пути", 181)
    assert found["via"] == "search"
    assert {"track_name": "Ути-Пути"} in asked


def test_a_refusal_in_the_middle_of_a_search_is_not_a_miss(monkeypatch):
    monkeypatch.setattr(lyrics, "_search", REAL_SEARCH)
    answers = iter([Answer(200, []), Answer(503, None)])
    monkeypatch.setattr(lyrics, "_get", lambda url, params=None: next(answers))
    assert lyrics._search("A", "T", 100) is None


def test_the_found_text_remembers_where_it_came_from(monkeypatch):
    monkeypatch.setattr(lyrics, "_ask", lambda *a: {})
    monkeypatch.setattr(
        lyrics,
        "_search",
        lambda *a: {**entry("Travis Scott", "SICKO MODE", 312, id=7), "via": "search"},
    )
    result = lyrics.for_track(ROW["path"], row=ROW)
    assert result["via"] == "search"
    assert result["matched"]["id"] == 7


# ---------------------------------------------------------------------------
# Ручной выбор
# ---------------------------------------------------------------------------


def test_candidates_put_the_fitting_record_first_and_keep_the_rest(monkeypatch):
    monkeypatch.setattr(
        lyrics,
        "_get",
        lambda url, params=None: Answer(
            200,
            [
                entry("Someone", "SICKO MODE", 200, id=1),
                entry("Travis Scott", "SICKO MODE", 312, id=2),
                entry("Travis Scott", "Empty", 312, id=3, synced="", plain=""),
            ],
        ),
    )
    found = lyrics.candidates(ROW)
    assert [item["id"] for item in found] == [2, 1]
    assert found[0]["fits"] is True and found[1]["fits"] is False
    assert found[0]["preview"] == "строка"


def test_candidates_say_when_the_catalogue_is_silent(monkeypatch):
    monkeypatch.setattr(lyrics, "_get", lambda url, params=None: Answer(503, None))
    assert lyrics.candidates(ROW) is None


def test_a_chosen_text_is_kept_and_not_asked_again(monkeypatch):
    monkeypatch.setattr(
        lyrics,
        "_get",
        lambda url, params=None: Answer(200, entry("Travis Scott", "SICKO MODE", 312, id=9)),
    )
    result = lyrics.choose(ROW["path"], ROW, 9)
    assert result["found"] is True and result["chosen"] is True
    assert not lyrics.needs_lookup(ROW["path"])


def test_own_text_with_timestamps_is_synced_and_without_is_plain():
    timed = "[00:01.00] раз\n[00:02.00] два\n[00:03.00] три"
    result = lyrics.save_custom(ROW["path"], ROW, timed)
    assert [line["line"] for line in result["synced"]] == ["раз", "два", "три"]
    assert result["plain"] == "раз\nдва\nтри"

    plain = lyrics.save_custom(ROW["path"], ROW, "просто\nтекст")
    assert plain["synced"] == [] and plain["plain"] == "просто\nтекст"
    assert plain["source"] == "manual" and plain["found"] is True


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from adder import config, covers, navidrome, playlists, runtime

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(playlists, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(covers, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    (config.LIBRARY / "A" / "B").mkdir(parents=True)
    (config.LIBRARY / "A" / "B" / "t.m4a").write_bytes(b"x")
    monkeypatch.setattr(library, "library_index", lambda: [ROW])

    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def test_own_text_is_saved_and_then_served(client):
    saved = client.post("/api/lyrics/custom", json={"path": ROW["path"], "text": "мой текст"})
    assert saved.status_code == 200
    assert client.get("/api/lyrics", params={"path": ROW["path"]}).json()["plain"] == "мой текст"


def test_empty_own_text_is_refused(client):
    assert (
        client.post("/api/lyrics/custom", json={"path": ROW["path"], "text": "  "}).status_code
        == 400
    )


def test_a_path_outside_the_library_is_refused(client):
    for path in ("../../etc/passwd", "outside:../x"):
        response = client.post("/api/lyrics/custom", json={"path": path, "text": "x"})
        assert response.status_code in (400, 404), path
        assert client.get("/api/lyrics/candidates", params={"path": path}).status_code in (400, 404)


def test_candidates_endpoint_reports_a_silent_catalogue(client, monkeypatch):
    monkeypatch.setattr(lyrics, "candidates", lambda row, query="": None)
    assert client.get("/api/lyrics/candidates", params={"path": ROW["path"]}).status_code == 502


def test_a_one_second_retry_after_does_not_shorten_the_backoff(monkeypatch):
    monkeypatch.setattr(library, "library_index", lambda: rows(3))
    answers = iter([None, entry("A", "x", 100), entry("A", "x", 100)])
    monkeypatch.setattr(lyrics, "_ask", lambda *a: next(answers))
    monkeypatch.setattr(lyrics, "last_retry_after", 1.0)
    waited = []
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda seconds: waited.append(seconds) or False)
    lyrics.backfill(stop=stop, pause=0, backoff=(30,))
    assert waited[0] == 30


def test_a_lookup_that_finishes_after_a_manual_choice_does_not_overwrite_it(monkeypatch):
    lyrics.save_custom(ROW["path"], ROW, "мой текст")
    monkeypatch.setattr(lyrics, "_ask", lambda *a: entry("Travis Scott", "SICKO MODE", 312))
    # Поиск, начатый раньше выбора, пишет свой результат после него.
    stored = lyrics._store(ROW["path"], lyrics._result(entry("X", "Y", 1), ROW, "lrclib"))
    assert stored["plain"] == "мой текст"
    assert lyrics.for_track(ROW["path"], row=ROW)["plain"] == "мой текст"


def test_own_text_drops_lrc_tags_and_keeps_brackets_inside_lines():
    text = "[ar:Кто-то]\n[ti:Что-то]\nвстреча в [10:30] у метро\nвторая строка"
    result = lyrics.save_custom(ROW["path"], ROW, text)
    assert result["plain"] == "встреча в [10:30] у метро\nвторая строка"
    assert result["synced"] == []
    assert result["via"] == "manual"


def test_own_text_of_only_timestamps_is_refused():
    with pytest.raises(ValueError):
        lyrics.save_custom(ROW["path"], ROW, "[00:01.00]\n[00:02.00]\n[00:03.00]")


def test_candidates_survive_records_without_id_or_with_a_strange_length(monkeypatch):
    monkeypatch.setattr(
        lyrics,
        "_get",
        lambda url, params=None: Answer(
            200,
            [
                entry("Travis Scott", "SICKO MODE", 312),
                entry("Travis Scott", "SICKO MODE", "три минуты", id=5),
            ],
        ),
    )
    found = lyrics.candidates(ROW)
    assert [item["id"] for item in found] == [5]
