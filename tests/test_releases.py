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


def baseline(artists=None):
    """Первый проход по артистам уже был: дальше всё найденное у них — «новое».

    ``artists`` — имя → номер Deezer; по умолчанию Seed с номером 1."""
    known = {
        releases._key(name): {"id": rid, "at": NOW - 86400, "checked": True}
        for name, rid in (artists or {"Seed": "1"}).items()
    }
    runtime.NEW_RELEASES_FILE.write_text(
        json.dumps({"artists": known, "releases": [], "checked_at": NOW - 86400}),
        encoding="utf-8",
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
    assert "Seed" not in deezer["find"]


def test_a_pass_cut_short_on_the_first_night_baselines_only_who_was_asked(deezer, monkeypatch):
    """Review: a first pass broken by the network made everyone after the
    break «new» the next night — two months of releases at once."""
    library = LIBRARY + [row("Second", "Song", album="Old Two")]
    ALBUMS["2"] = [
        {"id": 21, "title": "Two Fresh", "record_type": "album", "release_date": day(8)},
        {"id": 22, "title": "Old Two", "record_type": "album", "release_date": day(30)},
    ]
    monkeypatch.setattr(
        discography,
        "find_artist",
        lambda name: (
            {"id": {"Seed": "1", "Second": "2"}[name]} if name in ("Seed", "Second") else None
        ),
    )
    real_get = similar._get
    down = [True]  # the first question about Second, and only that one

    def flaky(client, url, params):
        if "/artist/2/" in url and down:
            down.pop()
            raise similar._Unreachable("ответ 503")
        return real_get(client, url, params)

    monkeypatch.setattr(similar, "_get", flaky)
    try:
        assert releases.check(library, now=NOW)["complete"] is False
        report = releases.check(library, now=NOW + 86400)
    finally:
        del ALBUMS["2"]
    assert report["complete"] is True
    # Second got its first look only tonight: its releases are a baseline too.
    assert report["new"] == []
    assert "Two Fresh" in [r["title"] for r in releases.shelf()]


def test_an_artist_id_empty_for_a_month_is_looked_up_again(deezer, monkeypatch):
    """Review round 2: a merged or deleted Deezer artist answers /albums with an
    empty list, not an error; it must not stay silent for good."""
    library = LIBRARY + [row("Gone", "Song")] * 3
    baseline({"Gone": "404", "Seed": "1"})
    report = releases.check(library, now=NOW)
    assert report["complete"] is True
    assert [r["title"] for r in report["new"]] == ["Fresh Album"]
    assert releases.load()["artists"]["gone"]["empty_since"] == NOW

    releases.check(library, now=NOW + 20 * 86400)
    assert "Gone" not in deezer["find"]
    releases.check(library, now=NOW + 31 * 86400)
    assert "gone" not in releases.load()["artists"]
    releases.check(library, now=NOW + 32 * 86400)
    assert deezer["find"].count("Gone") == 1


def test_deezer_no_data_for_an_artist_is_forgotten_not_an_outage(deezer, monkeypatch):
    library = LIBRARY + [row("Gone", "Song")] * 3
    baseline({"Gone": "404", "Seed": "1"})
    real_get = similar._get

    def get(client, url, params):
        if "/artist/404/" in url:
            raise similar._NoData("ошибка Deezer: {'code': 800}")
        return real_get(client, url, params)

    monkeypatch.setattr(similar, "_get", get)
    report = releases.check(library, now=NOW)
    assert report["complete"] is True
    assert "gone" not in releases.load()["artists"]


def test_deezer_no_data_is_told_apart_from_a_quota_error(monkeypatch):
    answers = iter(
        [
            {"error": {"type": "DataException", "message": "no data", "code": 800}},
            {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}},
        ]
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=next(answers)))
    monkeypatch.setattr(similar, "MIN_INTERVAL", 0.0)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(similar._NoData):
            similar._get(client, "https://api.deezer.com/artist/1/albums", {})
        with pytest.raises(similar._Unreachable) as quota:
            similar._get(client, "https://api.deezer.com/artist/1/albums", {})
    assert not isinstance(quota.value, similar._NoData)


def test_a_namesake_is_not_adopted(deezer, monkeypatch):
    """Review: the most famous «X» on Deezer is not the small «X» of the library."""
    monkeypatch.setattr(discography, "find_artist", lambda name: {"id": "7", "name": name})
    ALBUMS["7"] = [{"id": 71, "title": "Stadium", "record_type": "album", "release_date": day(4)}]
    real_get = similar._get

    def get(client, url, params):
        if "/artist/7/top" in url:
            return [{"title": "Their Hit"}]
        return real_get(client, url, params)

    monkeypatch.setattr(similar, "_get", get)
    try:
        report = releases.check([row("Tiny", "My Song")], now=NOW)
    finally:
        del ALBUMS["7"]
    assert releases.shelf() == []
    assert report["unknown"] == 1
    entry = releases.load()["artists"]["tiny"]
    assert entry["id"] is None and entry["namesake"] == "7"


def test_the_same_name_is_adopted_when_a_title_matches_its_top_tracks(deezer, monkeypatch):
    monkeypatch.setattr(discography, "find_artist", lambda name: {"id": "7", "name": name})
    ALBUMS["7"] = [{"id": 71, "title": "Stadium", "record_type": "album", "release_date": day(4)}]
    real_get = similar._get

    def get(client, url, params):
        if "/artist/7/top" in url:
            return [{"title": "My Song"}]
        return real_get(client, url, params)

    monkeypatch.setattr(similar, "_get", get)
    try:
        releases.check([row("Tiny", "My Song")], now=NOW)
    finally:
        del ALBUMS["7"]
    assert [r["title"] for r in releases.shelf()] == ["Stadium"]
    assert releases.load()["artists"]["tiny"]["id"] == "7"


def test_a_release_that_drops_off_and_returns_is_not_new_again(deezer):
    """Review: an artist slipping out of the top and back made its releases
    «new» a second time — a second notice, a second download."""
    baseline()
    assert releases.check(LIBRARY, now=NOW)["new"]
    without_seed = [r for r in LIBRARY if not r["artist"].startswith("Seed")]
    releases.check(without_seed, now=NOW + 86400)
    assert releases.shelf() == []
    assert releases.check(LIBRARY, now=NOW + 2 * 86400)["new"] == []


def test_valid_json_of_the_wrong_shape_is_an_empty_shelf(deezer):
    for broken in (
        {"releases": None},
        {"releases": [1, "x", {"id": 5}]},
        {"artists": []},
        {"artists": {"seed": "1"}, "memory": {"11": None}},
    ):
        runtime.NEW_RELEASES_FILE.write_text(json.dumps(broken), encoding="utf-8")
        assert releases.shelf() == []
        releases.check(LIBRARY, now=NOW)  # and the night does not fail on it


def test_a_release_downloaded_by_hand_and_deleted_is_not_new_later(deezer):
    """Review round 2: first seen while already in the library, then removed."""
    baseline()
    with_it = LIBRARY + [row("Seed", "x", album="Fresh Album")]
    assert releases.check(with_it, now=NOW)["new"] == []
    assert releases.check(LIBRARY, now=NOW + 86400)["new"] == []
    assert releases.load()["releases"][0]["queued"] is True


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
    baseline({f"A{i}": f"A{i}" for i in range(12)})
    releases.check([row(f"A{i}", "t") for i in range(12)], now=NOW)

    assert len(times) == 12
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    # Время ловит обработчик, после работы клиента, и зазор дрожит на доли
    # миллисекунды (было 0.0188 под нагрузкой). Без паузы он был бы ~0.001.
    assert min(gaps) >= 0.015


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


def _mock_service(monkeypatch, status_for=lambda body: 200):
    monkeypatch.setattr(config, "NEW_RELEASES_AUTO", True)
    monkeypatch.setattr(releases, "_service_token", lambda: "tok")
    posts = []

    def handler(request):
        body = json.loads(request.content)
        posts.append(body)
        return httpx.Response(status_for(body), json={})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return posts


def _shelf_of(*items):
    runtime.NEW_RELEASES_FILE.write_text(
        json.dumps({"artists": {}, "releases": list(items), "checked_at": NOW}), encoding="utf-8"
    )


def _release(rid, date):
    return {"id": rid, "artist": "Seed", "title": f"R{rid}", "kind": "album", "date": date}


def test_one_refused_release_does_not_block_the_rest(deezer, monkeypatch):
    """Review: one 422 broke the loop, every night, for two months."""
    posts = _mock_service(
        monkeypatch, lambda body: 422 if body["tracks"][0]["album"] == "31" else 200
    )
    _shelf_of(_release("31", day(3)), _release("32", day(2)))
    assert releases.queue_downloads() == 1
    assert [p["tracks"][0]["album"] for p in posts] == ["31", "32"]
    # The refused one is not asked again tomorrow.
    assert all(r["queued"] for r in releases.load()["releases"])
    assert releases.queue_downloads() == 0


def test_a_service_that_turns_the_token_away_is_tried_again(deezer, monkeypatch):
    _mock_service(monkeypatch, lambda body: 401)
    _shelf_of(_release("31", day(3)), _release("32", day(2)))
    assert releases.queue_downloads() == 0
    assert not any(r.get("queued") for r in releases.load()["releases"])


def test_a_release_of_only_versions_is_done_but_not_counted_as_sent(deezer, monkeypatch):
    posts = _mock_service(monkeypatch)
    real_get = similar._get

    def get(client, url, params):
        if "/album/" in url:
            return [{"title": "One (Remix)", "duration": 200, "artist": {"name": "Seed"}}]
        return real_get(client, url, params)

    monkeypatch.setattr(similar, "_get", get)
    _shelf_of(_release("31", day(3)))
    assert releases.queue_downloads() == 0
    assert posts == []
    assert releases.load()["releases"][0]["queued"] is True


def test_a_release_not_out_yet_waits_for_its_date(deezer, monkeypatch):
    posts = _mock_service(monkeypatch)
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    _shelf_of(_release("31", tomorrow))
    assert releases.queue_downloads() == 0
    assert posts == []


def test_turning_auto_download_on_does_not_fetch_the_backlog(deezer, monkeypatch):
    """Review: what was found while it was off stays off."""
    baseline()
    releases.check(LIBRARY, now=NOW)  # NEW_RELEASES_AUTO is off
    posts = _mock_service(monkeypatch)
    assert releases.queue_downloads() == 0
    assert posts == []


def test_the_service_address_follows_host(monkeypatch):
    monkeypatch.setattr(config, "HOST", "192.168.1.5")
    assert releases._service_url() == f"http://192.168.1.5:{config.PORT}/api/artists/import"
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    assert releases._service_url().startswith("http://127.0.0.1:")
    monkeypatch.setattr(config, "HOST", "::1")
    assert releases._service_url().startswith("http://[::1]:")


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
