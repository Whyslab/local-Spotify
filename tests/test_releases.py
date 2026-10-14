"""Новые релизы артистов фонотеки (план 4c.2).

Deezer подменяется: ``discography.find_artist`` — кто есть кто,
``similar._get`` — ответы на адреса альбомов и треков.
"""

import datetime
import json
import time

import httpx
import pytest

from adder import config, discography, notify, releases, runtime, similar

NOW = datetime.datetime(2026, 10, 14, 12, 0).timestamp()


def day(days_ago: int) -> str:
    return (datetime.datetime.fromtimestamp(NOW) - datetime.timedelta(days=days_ago)).strftime(
        "%Y-%m-%d"
    )


def row(artist, title, album=""):
    return {"path": f"{artist}/{title}.m4a", "artist": artist, "title": title, "album": album}


LIBRARY = [
    row("Seed", "Had Single"),
    row("Seed", "Song", album="Old Album"),
    row("Seed", "Other"),
    row("Seed • Guest", "Feat"),
    row("Channel TV", "Clip"),
]

ALBUMS = {
    "1": [
        {"id": 11, "title": "Fresh Album", "record_type": "album", "release_date": day(10)},
        {"id": 12, "title": "Ancient", "record_type": "album", "release_date": day(200)},
        {"id": 13, "title": "Had Single", "record_type": "single", "release_date": day(5)},
        {"id": 14, "title": "Fresh (Sped Up)", "record_type": "single", "release_date": day(3)},
        {"id": 15, "title": "Hits", "record_type": "compile", "release_date": day(3)},
        {"id": 16, "title": "Old Album", "record_type": "album", "release_date": day(20)},
    ],
}


@pytest.fixture
def deezer(monkeypatch):
    asked = {"find": [], "get": []}

    def find_artist(name):
        asked["find"].append(name)
        return {"id": "1", "name": name} if name == "Seed" else None

    def get(client, url, params):
        asked["get"].append(url)
        if "/albums" in url:
            return ALBUMS.get(url.split("/artist/")[1].split("/")[0], [])
        if "/album/" in url:
            return [
                {"title": "One", "duration": 180, "artist": {"name": "Seed"}},
                {"title": "One (Remix)", "duration": 200, "artist": {"name": "Seed"}},
            ]
        raise AssertionError(url)

    monkeypatch.setattr(discography, "find_artist", find_artist)
    monkeypatch.setattr(similar, "_get", get)
    return asked


def baseline():
    """Первый проход уже был: дальше всё найденное — «новое»."""
    runtime.NEW_RELEASES_FILE.write_text(
        json.dumps({"artists": {}, "releases": [], "checked_at": NOW - 86400}), encoding="utf-8"
    )


def test_only_recent_unheld_releases_are_new(deezer):
    baseline()
    report = releases.check(LIBRARY, now=NOW)

    assert [r["title"] for r in report["new"]] == ["Fresh Album"]
    assert [r["title"] for r in releases.shelf()] == ["Fresh Album"]
    shown = releases.shelf()[0]
    assert shown["artist"] == "Seed" and shown["kind"] == "album" and shown["date"] == day(10)


def test_a_release_is_new_only_once(deezer):
    baseline()
    releases.check(LIBRARY, now=NOW)
    again = releases.check(LIBRARY, now=NOW + 86400)
    assert again["new"] == []
    assert [r["title"] for r in releases.shelf()] == ["Fresh Album"]


def test_a_release_added_to_the_library_leaves_the_shelf(deezer):
    baseline()
    releases.check(LIBRARY, now=NOW)
    releases.check(LIBRARY + [row("Seed", "x", album="Fresh Album")], now=NOW + 86400)
    assert releases.shelf() == []


def test_the_first_pass_is_a_baseline(deezer, monkeypatch):
    report = releases.check(LIBRARY, now=NOW)
    assert report["new"] == []
    assert [r["title"] for r in releases.shelf()] == ["Fresh Album"]
    # И не уходит в автоскачивание.
    monkeypatch.setattr(config, "NEW_RELEASES_AUTO", True)
    assert releases.queue_downloads() == 0


def test_an_artist_deezer_does_not_know_by_name_is_skipped_for_a_month(deezer):
    baseline()
    report = releases.check(LIBRARY, now=NOW)
    assert report["unknown"] == 1  # Channel TV
    assert report["asked"] == 1
    assert sum(1 for name in deezer["find"] if name == "Channel TV") == 1

    releases.check(LIBRARY, now=NOW + 29 * 86400)
    assert sum(1 for name in deezer["find"] if name == "Channel TV") == 1
    releases.check(LIBRARY, now=NOW + 31 * 86400)
    assert sum(1 for name in deezer["find"] if name == "Channel TV") == 2
    # Найденный артист повторно не ищется вовсе.
    assert deezer["find"].count("Seed") == 1


def test_artists_are_the_biggest_first_and_capped(monkeypatch):
    rows = [row(f"A{i}", f"t{j}") for i in range(5) for j in range(i + 1)]
    rows += [row("A0 • Guest", "feat")]
    assert releases.library_artists(rows, limit=3) == ["A4", "A3", "A2"]
    assert releases.library_artists(rows)[-1] in {"A0", "A1"}
    assert len(releases.library_artists(rows)) == 5


def test_requests_are_paced(monkeypatch):
    """Не чаще одного запроса в similar.MIN_INTERVAL — лимит Deezer 50 за 5 с."""
    monkeypatch.setattr(similar, "MIN_INTERVAL", 0.02)
    monkeypatch.setattr(discography, "find_artist", lambda name: {"id": name, "name": name})
    times = []

    def handler(request):
        times.append(time.monotonic())
        return httpx.Response(200, json={"data": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    baseline()
    releases.check([row(f"A{i}", "t") for i in range(12)], now=NOW)

    assert len(times) == 12
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= 0.019


def test_deezer_going_silent_keeps_what_was_found(deezer, monkeypatch):
    baseline()
    releases.check(LIBRARY, now=NOW)

    def down(client, url, params):
        raise similar._Unreachable("ответ 503")

    monkeypatch.setattr(similar, "_get", down)
    report = releases.check(LIBRARY, now=NOW + 86400)
    assert report["complete"] is False
    assert [r["title"] for r in releases.shelf()] == ["Fresh Album"]


def test_a_broken_file_is_an_empty_shelf():
    runtime.NEW_RELEASES_FILE.write_text("{not json", encoding="utf-8")
    assert releases.shelf() == []


def test_the_notification_names_the_first_few(monkeypatch):
    shown = []
    monkeypatch.setattr(notify, "_show", lambda *args: shown.append(args))
    monkeypatch.setattr(config, "DESKTOP_NOTIFICATIONS", True)
    fresh = [{"artist": "A", "title": f"T{i}"} for i in range(5)]

    releases.tell(fresh)

    summary, body, urgent = shown[0]
    assert summary == "Новые релизы: 5"
    assert body.splitlines() == ["A — T0", "A — T1", "A — T2", "и ещё 2"]

    shown.clear()
    releases.tell([])
    monkeypatch.setattr(config, "DESKTOP_NOTIFICATIONS", False)
    releases.tell(fresh)
    assert shown == []


def test_auto_download_is_off_by_default(deezer):
    baseline()
    releases.check(LIBRARY, now=NOW)
    assert config.NEW_RELEASES_AUTO is False
    assert releases.queue_downloads() == 0


def test_auto_download_hands_new_tracks_to_the_service_once(deezer, monkeypatch):
    monkeypatch.setattr(config, "NEW_RELEASES_AUTO", True)
    monkeypatch.setattr(releases, "_service_token", lambda: "tok")
    posts = []

    def handler(request):
        posts.append(
            (str(request.url), request.headers["authorization"], json.loads(request.content))
        )
        return httpx.Response(200, json={"added": 1})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    baseline()
    releases.check(LIBRARY, now=NOW)

    assert releases.queue_downloads() == 1
    url, auth, body = posts[0]
    assert url == f"http://127.0.0.1:{config.PORT}/api/artists/import"
    assert auth == "Bearer tok"
    # Версии не берутся, номер альбома — для обложки и тегов.
    assert body == {"tracks": [{"artist": "Seed", "title": "One", "duration": 180, "album": "11"}]}

    assert releases.queue_downloads() == 0
    assert len(posts) == 1


def test_an_unreachable_service_is_tried_again_next_night(deezer, monkeypatch):
    monkeypatch.setattr(config, "NEW_RELEASES_AUTO", True)
    monkeypatch.setattr(releases, "_service_token", lambda: "tok")

    def refuse(request):
        raise httpx.ConnectError("refused")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(refuse), **kw)
    )
    baseline()
    releases.check(LIBRARY, now=NOW)
    assert releases.queue_downloads() == 0
    assert not releases.load()["releases"][0].get("queued")


def _export_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "export_shelves.py"
    spec = importlib.util.spec_from_file_location("export_shelves_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_failing_release_check_does_not_fail_the_night(capsys):
    from types import SimpleNamespace

    def boom(rows):
        raise OSError("диск полон")

    library = SimpleNamespace(library_index=lambda: LIBRARY)
    fake = SimpleNamespace(check=boom, tell=None, queue_downloads=None)
    _export_script().new_releases(library, fake)
    assert "Новые релизы: не вышло (диск полон)" in capsys.readouterr().out


def test_the_night_reports_what_it_found(deezer, capsys):
    baseline()
    library = type("L", (), {"library_index": staticmethod(lambda: LIBRARY)})
    _export_script().new_releases(library, releases)
    out = capsys.readouterr().out
    assert "спрошено артистов 1, неузнанных 1, новых 1" in out
