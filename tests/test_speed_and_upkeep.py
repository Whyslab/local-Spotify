"""Скорость и уборка (разбор 24.11.2026): каждый тест падает на прежнем коде.

- Deezer: один поиск артиста на похожих и популярные треки; сбой связи
  помнится минуты; ночной прогрев кэша.
- Поиск YouTube: оба поиска разом, из памяти на повтор, запасной процесс.
- Умное по звучанию: текст для языка не перечитывается, если файл тот же.
- Уборка: отправленные прослушивания, старые миниатюры, гонка в чистке
  переходов, прогрев главной без скачиваний, /health без Navidrome.
"""

import json
import os
import time

import httpx
import pytest

from adder import app as app_module
from adder import config, db, lyrics, outside, runtime, similar, sonic, sources, thumbs, transitions

# Настоящий поиск внутри процесса: conftest подменяет его в каждом тесте,
# а этот файл импортируется раньше — тут он ещё свой.
REAL_SEARCH_IN_PROCESS = sources._search_in_process


# ---------------------------------------------------------------------------
# Deezer


@pytest.fixture()
def deezer(monkeypatch):
    """Подменённый Deezer: считает запросы по адресу."""
    calls: list[str] = []
    real = httpx.Client
    state = {"down": False}

    def handler(request):
        calls.append(request.url.path)
        if state["down"]:
            return httpx.Response(503)
        if request.url.path == "/search/artist":
            return httpx.Response(200, json={"data": [{"id": 7, "name": "Markul", "nb_fan": 9}]})
        if request.url.path.endswith("/related"):
            return httpx.Response(200, json={"data": [{"name": "Friend"}]})
        if request.url.path.endswith("/top"):
            return httpx.Response(
                200,
                json={"data": [{"title": "Song", "artist": {"name": "Markul"}, "duration": 200}]},
            )
        return httpx.Response(404)

    monkeypatch.setattr(similar, "MIN_INTERVAL", 0)
    monkeypatch.setattr(
        similar.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler))
    )
    return type("Deezer", (), {"calls": calls, "state": state})


def test_one_artist_search_serves_similar_and_top_tracks(deezer, tmp_path):
    assert similar.similar_artists("Markul", tmp_path) == ["Friend"]
    assert similar.top_tracks("Markul", tmp_path)[0]["title"] == "Song"
    assert deezer.calls.count("/search/artist") == 1


def test_a_failed_artist_is_not_asked_again_for_a_few_minutes(deezer, tmp_path):
    deezer.state["down"] = True
    assert similar.similar_artists("Markul", tmp_path) == []
    asked = len(deezer.calls)
    assert similar.similar_artists("Markul", tmp_path) == []
    assert len(deezer.calls) == asked
    # Сбой не записан в кэш: через DOWN_SECONDS спросим снова и запомним ответ.
    assert list(tmp_path.iterdir()) == []
    deezer.state["down"] = False
    similar._down.clear()
    assert similar.similar_artists("Markul", tmp_path) == ["Friend"]


def test_warm_cache_asks_once_and_then_only_reads(deezer, tmp_path, monkeypatch):
    monkeypatch.setattr(outside, "_similar_cache", lambda: tmp_path)
    rows = [{"artist": "Markul"}, {"artist": "Markul • Guest"}]
    first = outside.warm_cache(rows, budget=60)
    assert first["complete"] and first["asked"] == 3  # похожие Markul, топ Markul, топ Friend
    assert similar.cached("Friend", tmp_path, "top2:")
    calls = len(deezer.calls)
    again = outside.warm_cache(rows, budget=60)
    assert again == {"asked": 0, "failed": 0, "complete": True}
    assert len(deezer.calls) == calls


def test_warm_cache_gives_up_when_deezer_is_down(deezer, tmp_path, monkeypatch):
    monkeypatch.setattr(outside, "_similar_cache", lambda: tmp_path)
    deezer.state["down"] = True
    rows = [{"artist": f"Artist {i}"} for i in range(20)]
    report = outside.warm_cache(rows, budget=60)
    assert not report["complete"]
    assert report["failed"] == outside.WARM_FAILURES


def test_warm_cache_stops_at_its_budget(deezer, tmp_path, monkeypatch):
    monkeypatch.setattr(outside, "_similar_cache", lambda: tmp_path)
    report = outside.warm_cache([{"artist": "Markul"}], budget=0)
    assert report == {"asked": 0, "failed": 0, "complete": False}


# ---------------------------------------------------------------------------
# Поиск YouTube


class _Done:
    returncode = 0
    stderr = ""

    def __init__(self, ids):
        self.stdout = json.dumps({"entries": [{"id": i, "title": i} for i in ids]})


def test_search_is_remembered_for_a_while(monkeypatch):
    runs = []
    monkeypatch.setattr(
        sources.ingest, "run_yt_dlp", lambda cmd, timeout: runs.append(cmd) or _Done(["a1"])
    )
    first = sources.search_youtube("Markul Song")
    second = sources.search_youtube("markul song ")
    assert first == second and len(runs) == 1
    # Изменённый ответ не портит запомненный.
    second[0]["title"] = "changed"
    assert sources.search_youtube("Markul Song")[0]["title"] == "a1"


def test_search_runs_in_the_service_when_it_can(monkeypatch):
    monkeypatch.setattr(config, "COOKIES_FROM_BROWSER", "")
    monkeypatch.setattr(
        sources, "_search_in_process", lambda query, count: [{"id": "fast", "title": "x"}]
    )
    monkeypatch.setattr(
        sources.ingest, "run_yt_dlp", lambda cmd, timeout: pytest.fail("no process wanted")
    )
    assert [row["url"][-4:] for row in sources.search_youtube("q")] == ["fast"]


def test_search_falls_back_to_a_process(monkeypatch):
    monkeypatch.setattr(config, "COOKIES_FROM_BROWSER", "")
    monkeypatch.setattr(sources, "_search_in_process", lambda query, count: None)
    monkeypatch.setattr(sources.ingest, "run_yt_dlp", lambda cmd, timeout: _Done(["slow"]))
    assert [row["url"][-4:] for row in sources.search_youtube("q")] == ["slow"]


def test_cookies_keep_the_search_in_a_process(monkeypatch):
    monkeypatch.setattr(config, "COOKIES_FROM_BROWSER", "firefox")
    monkeypatch.setattr(
        sources, "_search_in_process", lambda query, count: pytest.fail("cookies need the CLI")
    )
    monkeypatch.setattr(sources.ingest, "run_yt_dlp", lambda cmd, timeout: _Done(["cli1"]))
    assert [row["url"][-4:] for row in sources.search_youtube("q")] == ["cli1"]


def test_a_failing_studio_search_does_not_break_the_results(monkeypatch):
    def broken(query, limit):
        raise RuntimeError("ytmusic down")

    monkeypatch.setattr(sources.studio, "search_query", broken)
    monkeypatch.setattr(sources.ingest, "run_yt_dlp", lambda cmd, timeout: _Done(["ok01"]))
    assert [row["url"][-4:] for row in sources.search_youtube("q")] == ["ok01"]


def test_a_cut_or_empty_search_is_not_remembered(monkeypatch):
    runs = []
    monkeypatch.setattr(
        sources.ingest, "run_yt_dlp", lambda cmd, timeout: runs.append(cmd) or _Done([])
    )
    assert sources.search_youtube("nothing") == []
    assert sources.search_youtube("nothing") == []
    assert len(runs) == 2

    def broken(query, limit):
        raise RuntimeError("ytmusic down")

    monkeypatch.setattr(sources.studio, "search_query", broken)
    monkeypatch.setattr(
        sources.ingest, "run_yt_dlp", lambda cmd, timeout: runs.append(cmd) or _Done(["x1"])
    )
    sources.search_youtube("cut")
    sources.search_youtube("cut")
    assert len(runs) == 4


def _stub_ytdlp(monkeypatch, extract):
    import sys
    import types

    class Stub:
        def __init__(self, options):
            Stub.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            return extract(url)

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Stub))
    # Флаг остановки поднимают тесты, запускавшие приложение, — тут свой.
    import threading

    monkeypatch.setattr(runtime, "shutdown_event", threading.Event())
    monkeypatch.setattr(sources, "SEARCH_GAP_SECONDS", 0.0)
    return Stub


def test_the_in_process_search_reads_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "CACHE_DIR", tmp_path / "cache")
    stub = _stub_ytdlp(monkeypatch, lambda url: {"entries": [{"id": "a"}, None, "junk"]})
    assert REAL_SEARCH_IN_PROCESS("q", 3) == [{"id": "a"}]
    assert stub.options["cachedir"] == str(tmp_path / "cache" / "yt-dlp")
    assert stub.options["source_address"] == "0.0.0.0"


def test_a_rate_limited_search_does_not_ask_again(monkeypatch):
    def limited(url):
        raise RuntimeError("ERROR: HTTP Error 429: Too Many Requests")

    _stub_ytdlp(monkeypatch, limited)
    monkeypatch.setattr(sources, "_search_in_process", REAL_SEARCH_IN_PROCESS)
    monkeypatch.setattr(config, "COOKIES_FROM_BROWSER", "")
    monkeypatch.setattr(
        sources.ingest, "run_yt_dlp", lambda cmd, timeout: pytest.fail("429 is not retried")
    )
    with pytest.raises(RuntimeError, match="429"):
        sources.search_youtube("q")
    # Пауза стоит: следующие поиски не долбят YouTube под лимитом.
    assert runtime.youtube_pause_left() > 0


def test_in_process_searches_keep_a_gap(monkeypatch):
    _stub_ytdlp(monkeypatch, lambda url: {"entries": []})
    monkeypatch.setattr(sources, "SEARCH_GAP_SECONDS", 0.3)
    monkeypatch.setattr(sources, "_last_search", [0.0])
    started = time.monotonic()
    REAL_SEARCH_IN_PROCESS("a", 1)
    REAL_SEARCH_IN_PROCESS("b", 1)
    assert time.monotonic() - started >= 0.3


def test_a_failing_in_process_search_says_so(monkeypatch):
    def boom(url):
        raise RuntimeError("extractor broke")

    _stub_ytdlp(monkeypatch, boom)
    assert REAL_SEARCH_IN_PROCESS("q", 3) is None


def test_a_hanging_in_process_search_gives_up_in_time(monkeypatch):
    import threading

    release = threading.Event()
    _stub_ytdlp(monkeypatch, lambda url: release.wait(5) and {"entries": []})
    monkeypatch.setattr(sources, "IN_PROCESS_SECONDS", 0.3)
    started = time.monotonic()
    assert REAL_SEARCH_IN_PROCESS("q", 3) is None
    assert time.monotonic() - started < 1.5
    release.set()


def test_both_searches_run_at_once(monkeypatch):
    """Студийные и обычный поиск — одновременно, а не одно за другим."""

    def slow_studio(query, limit):
        time.sleep(0.4)
        return []

    def slow_youtube(cmd, timeout):
        time.sleep(0.4)
        return _Done(["both"])

    monkeypatch.setattr(sources.studio, "search_query", slow_studio)
    monkeypatch.setattr(sources.ingest, "run_yt_dlp", slow_youtube)
    started = time.monotonic()
    sources.search_youtube("q")
    assert time.monotonic() - started < 0.7


# ---------------------------------------------------------------------------
# Язык трека для умного по звучанию


def test_lyrics_for_the_language_are_read_once_per_file(monkeypatch, tmp_path):
    monkeypatch.setattr(lyrics, "CACHE_DIR", tmp_path)
    reads = []
    real = lyrics.plain_text
    monkeypatch.setattr(lyrics, "plain_text", lambda path: reads.append(path) or real(path))
    cached = lyrics._key("A/x.m4a")
    russian = "привет мир это длинная песня про ночной город " * 2
    cached.write_text(json.dumps({"found": True, "plain": russian}), encoding="utf-8")
    assert sonic._lyrics_verdict("A/x.m4a") == sonic.RU
    assert sonic._lyrics_verdict("A/x.m4a") == sonic.RU
    assert reads == ["A/x.m4a"]
    # Текст поменяли — читается заново.
    english = "hello world this is a long song about the night city " * 2
    cached.write_text(json.dumps({"found": True, "plain": english}), encoding="utf-8")
    later = time.time_ns() + 10**9
    os.utime(cached, ns=(later, later))
    assert sonic._lyrics_verdict("A/x.m4a") == sonic.OTHER
    # Без файла — «не знаю», и plain_text не зовётся вовсе.
    assert sonic._lyrics_verdict("B/none.m4a") is None
    assert reads == ["A/x.m4a", "A/x.m4a"]


@pytest.mark.parametrize(
    "text",
    ["Привет, world!", "ёЁ ӿ Ѐ ҂҃ 12 ₽", "", "1234", "Ѐ҂", "Ԁ кириллица-дополнение", "ⷠ"],
)
def test_cyrillic_share_counts_as_before(text):
    letters = [ch for ch in text if ch.isalpha()]
    expected = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ") / len(letters) if letters else 0.0
    assert sonic.is_cyrillic_text(text) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Уборка


def test_sent_listens_are_pruned_and_waiting_ones_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    db.db_init()
    old = int(time.time()) - 500 * 24 * 3600
    fresh = int(time.time()) - 3600
    for at, sent in [(old, 1), (old, 0), (fresh, 1)]:
        db.db_exec(
            "INSERT INTO listens(listened_at, artist, title, sent) VALUES(?, 'a', 't', ?)",
            (at, sent),
        )
    db.prune_play_history(400)
    left = db.db_query("SELECT listened_at, sent FROM listens ORDER BY id")
    assert [(row["listened_at"], row["sent"]) for row in left] == [(old, 0), (fresh, 1)]


def test_old_thumbnails_go_and_fresh_ones_stay(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "THUMB_DIR", tmp_path)
    stale, fresh = tmp_path / "old.jpg", tmp_path / "new.jpg"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    long_ago = time.time() - (thumbs.THUMB_KEEP_DAYS + 1) * 24 * 3600
    os.utime(stale, (long_ago, long_ago))
    assert thumbs.prune() == 1
    assert not stale.exists() and fresh.exists()


def test_a_plan_vanishing_during_pruning_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "TRANSITIONS_DIR", tmp_path)
    for i in range(transitions.KEEP + 2):
        (tmp_path / f"{i:064x}.json").write_text("{}")
    real_stat = type(tmp_path).stat
    victim = tmp_path / f"{3:064x}.json"

    def stat(self, *args, **kwargs):
        if self == victim:
            self.unlink(missing_ok=True)
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "stat", stat)
    transitions._prune()  # раньше: FileNotFoundError из sorted(key=stat)
    assert len(list(tmp_path.glob("*.json"))) <= transitions.KEEP


def test_warming_the_home_page_downloads_nothing(monkeypatch):
    prefetched = []
    monkeypatch.setattr(app_module.weekly, "prefetch", lambda tracks: prefetched.append(tracks))
    monkeypatch.setattr(app_module, "_home_payload", lambda prefetch: prefetched.append(prefetch))
    app_module._warm_home()
    assert prefetched == [False]


def test_health_does_not_ask_about_a_switched_off_navidrome(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(runtime, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(runtime, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "LIBRARY", tmp_path / "lib")
    (tmp_path / "lib").mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(app_module.navidrome, "configured", lambda: False)
    asked = []
    monkeypatch.setattr(app_module.navidrome, "pending_count", lambda: asked.append(1) or 5)
    with TestClient(app_module.app) as client:
        before = len(asked)
        body = client.get("/health", headers={"Authorization": "Bearer test-secret"}).json()
        assert len(asked) == before
    assert body["navidrome_pending"] == 0
