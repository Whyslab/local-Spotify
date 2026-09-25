"""The panel and player in a real browser (Playwright + Chromium).

A real service runs in a thread against a temporary library of a few short
tracks, with every outside service stubbed; Chromium opens the page the way a
person would. What the Python tests cannot see - that a button exists, that a
row turns into a form, what the <audio> element's volume actually is - is
checked here.

Needs the browser: `python -m playwright install chromium` (CI does this).
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from adder import (  # noqa: E402
    analysis,
    config,
    covers,
    enrich,
    ingest,
    library,
    listenbrainz,
    loudness,
    lyrics,
    navidrome,
    outside,
    playlists,
    runtime,
    similar,
    sync,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tone.m4a"
TOKEN = "ui-test-token"
XSS_TITLE = '<img src=x onerror="window.pwned=1">'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _track(root: Path, rel: str, artist: str, title: str, seconds: int, gain: float | None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-ac", "1", "-b:a", "32k", str(path)],
        check=True,
    )  # fmt: skip
    ingest.write_tags(path, enrich.TrackInfo(album=title, artists=[artist]), title, None, None)
    if gain is not None:
        loudness.write(path, gain, 0.5)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    from adder import app as app_module

    tmp = tmp_path_factory.mktemp("ui")
    root = tmp / "library"
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "API_TOKEN", TOKEN)
    mp.setattr(config, "LIBRARY", root)
    mp.setattr(config, "MAX_WORKERS", 0)
    mp.setattr(config, "DESKTOP_NOTIFICATIONS", False)
    mp.setattr(config, "LISTENBRAINZ_TOKEN", "")
    for name, value in {
        "DB_PATH": tmp / "tasks.db",
        "TMP_DIR": tmp / "tmp",
        "TRASH_DIR": tmp / "trash",
        "OUTSIDE_DIR": tmp / "outside",
        "THUMB_DIR": tmp / "thumbs",
        "CACHE_DIR": tmp / "cache",
    }.items():
        mp.setattr(runtime, name, value)
    mp.setattr(playlists, "HISTORY_DIR", tmp / "history")
    mp.setattr(covers, "COVERS_DIR", tmp / "covers")
    mp.setattr(lyrics, "CACHE_DIR", tmp / "lyrics")
    # Everything that would reach the network or another process at startup.
    mp.setattr(navidrome, "configured", lambda: False)
    mp.setattr(sync, "start", lambda: None)
    mp.setattr(lyrics, "start_backfill", lambda: None)
    mp.setattr(lyrics, "_ask", lambda *a, **k: {})
    mp.setattr(lyrics, "_search", lambda *a, **k: {})
    mp.setattr(listenbrainz, "start", lambda: None)
    mp.setattr(outside, "candidates", lambda *a, **k: [])
    mp.setattr(analysis, "analyse_track", lambda path: None)
    mp.setattr(similar, "_ask", lambda *a, **k: None)  # the home page's "similar artists"
    mp.setattr(similar, "_ask_top", lambda *a, **k: None)
    mp.setattr(enrich, "lookup", lambda a, t: None)
    mp.setattr(enrich, "musicbrainz_lookup", lambda a, t: None)
    mp.setattr(ingest, "check_dependencies", lambda: {"ffmpeg": "ok", "js_runtime": "ok"})

    # Opus for the tracks that are played: Playwright's Chromium has no AAC
    # decoder (a proprietary codec), while real Chrome and Safari have both.
    _track(root, "Loud Band/Singles/Loud.opus", "Loud Band", "Loud", 12, gain=-6.0)
    _track(root, "Quiet/Singles/Quiet.m4a", "Quiet", "Quiet", 12, gain=None)
    _track(root, "Evil/Singles/evil.m4a", "Evil", XSS_TITLE, 2, gain=None)
    library.invalidate_library_index()

    port = _free_port()
    uv = uvicorn.Server(
        uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not uv.started and time.time() < deadline:
        time.sleep(0.05)
    assert uv.started, "the service did not start"

    yield {"url": f"http://127.0.0.1:{port}", "root": root}

    uv.should_exit = True
    thread.join(timeout=15)
    mp.undo()
    runtime.shutdown_event.clear()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        yield instance
        instance.close()


@pytest.fixture()
def page(server, browser):
    context = browser.new_context()
    context.add_init_script(f"localStorage.setItem('token', '{TOKEN}');")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(server["url"] + "/")
    page.wait_for_function("typeof switchView === 'function'")
    yield page
    context.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"


def open_library(page):
    page.evaluate("switchView('viewLibrary')")
    page.wait_for_selector("#library .track")


def row(page, title):
    return page.locator("#library .track").filter(has_text=title).first


# ---------------------------------------------------------------------------


def test_the_library_lists_tracks_and_renders_titles_as_text(page):
    open_library(page)

    titles = page.locator("#library .track-title").all_inner_texts()

    assert {"Loud", "Quiet", XSS_TITLE} <= set(titles)
    assert page.evaluate("window.pwned") is None  # the title never became markup


def test_tags_are_edited_in_place(page, server):
    open_library(page)
    row(page, "Quiet").get_by_role("button", name="Изменить теги").click()

    form = page.locator("form.edit-tags")
    form.get_by_label("Название").fill("Quiet Storm")
    form.get_by_label("Альбом").fill("Night")
    form.get_by_role("button", name="Сохранить").click()

    page.wait_for_selector("#library .track-title:text-is('Quiet Storm')")
    tags = library.read_tags(server["root"] / "Quiet/Singles/Quiet.m4a")
    assert (tags["title"], tags["album"]) == ("Quiet Storm", "Night")


def test_replaygain_turns_a_loud_track_down_but_not_the_slider(page):
    open_library(page)
    row(page, "Loud").get_by_role("button", name="Играть").click()
    page.wait_for_function("!player.audio.paused && player.audio.currentTime > 0")
    assert page.evaluate("player.audio.error") is None

    volume = page.evaluate("player.audio.volume")
    slider = page.evaluate("document.getElementById('playerVolumeRange').value")

    assert volume == pytest.approx(10 ** (-6 / 20), abs=0.01)  # -6 dB
    assert slider == "100"  # the listener's own level is unchanged


def test_fades_at_the_ends_of_a_track(page):
    open_library(page)
    row(page, "Loud").get_by_role("button", name="Играть").click()
    page.wait_for_function("!player.audio.paused && player.audio.duration > 10")
    page.evaluate("setFade(3); player.audio.pause()")

    page.evaluate("player.audio.currentTime = player.audio.duration - 1.5; fadeTick()")
    near_end = page.evaluate("player.fadeLevel")
    page.evaluate("player.audio.currentTime = 6; fadeTick()")
    middle = page.evaluate("player.fadeLevel")

    assert near_end == pytest.approx(0.5, abs=0.1)
    assert middle == 1


def test_a_track_starts_silent_when_fades_are_on(page):
    # Regression: while the next track is loading its duration is NaN. The
    # fade used to be skipped then, so the track's first moment played at
    # full volume, dropped to silence once the duration arrived, and only
    # then faded in.
    open_library(page)
    row(page, "Loud").get_by_role("button", name="Играть").click()
    page.wait_for_function("!player.audio.paused && player.audio.duration > 10")
    page.evaluate("setFade(3); player.audio.pause()")

    # The state between two tracks: a new source, nothing known about it yet.
    page.evaluate("player.audio.removeAttribute('src'); player.audio.load(); fadeTick()")
    assert page.evaluate("Number.isNaN(player.audio.duration)")
    assert page.evaluate("player.fadeLevel") == 0


def test_a_fade_is_driven_by_a_fast_timer_only_while_it_runs(page):
    open_library(page)
    row(page, "Loud").get_by_role("button", name="Играть").click()
    page.wait_for_function("!player.audio.paused && player.audio.duration > 10")
    page.evaluate("setFade(3); player.audio.currentTime = 6; fadeTick()")
    assert page.evaluate("fadeTimer") is None  # middle of the track: no fade

    page.evaluate("player.audio.currentTime = player.audio.duration - 2; fadeTick()")
    assert page.evaluate("fadeTimer") is not None

    page.evaluate("player.audio.pause(); fadeTick()")
    assert page.evaluate("fadeTimer") is None


def test_the_lock_screen_shows_the_track_and_its_state(page):
    open_library(page)
    row(page, "Loud").get_by_role("button", name="Играть").click()
    page.wait_for_function("!player.audio.paused")
    page.wait_for_function("navigator.mediaSession.metadata !== null")

    meta = page.evaluate(
        "({title: navigator.mediaSession.metadata.title,"
        " artist: navigator.mediaSession.metadata.artist,"
        " state: navigator.mediaSession.playbackState})"
    )
    assert meta == {"title": "Loud", "artist": "Loud Band", "state": "playing"}

    # A pause from anywhere is shown on the lock screen too.
    page.evaluate("player.audio.pause()")
    page.wait_for_function("navigator.mediaSession.playbackState === 'paused'")


def test_the_next_track_starts_without_waiting_for_the_server(page):
    # The next track's link is fetched while this one plays, so a locked
    # iPhone does not have to wait on the network between two tracks.
    open_library(page)
    # A queue of two, built here: a click on a row can land before the whole
    # list has loaded, and then the queue holds that one track.
    page.evaluate(
        "playQueue([{path: 'Loud Band/Singles/Loud.opus', title: 'Loud', artist: 'Loud Band',"
        " duration: 12}, {path: 'Quiet/Singles/Quiet.m4a', title: 'Quiet', artist: 'Quiet',"
        " duration: 12}], 0)"
    )
    page.wait_for_function("!player.audio.paused")
    page.wait_for_function("nextStream !== null")

    # The new source is in place before nextTrack() even returns: nothing was
    # awaited in between. Without the prefetch it arrives a request later.
    switched = page.evaluate(
        "(() => { const before = player.audio.src; nextTrack();"
        " return player.audio.src !== before; })()"
    )
    assert switched is True

    # And without it, the same call has to wait for the server.
    page.evaluate("nextStream = null; player.audio.pause(); player.audio.currentTime = 0")
    waited = page.evaluate(
        "(() => { const before = player.audio.src; prevTrack();"
        " return player.audio.src === before; })()"
    )
    assert waited is True


def test_a_prefetched_link_must_cover_the_whole_next_track(page):
    open_library(page)
    now = page.evaluate("Date.now() / 1000")
    track = {"duration": 270}  # 4.5 minutes
    # A link fetched at the start of a 4.5-minute track has 300 s left at its end.
    assert page.evaluate("([s, t]) => linkLasts(s, t)", [{"expires": now + 300}, track]) is False
    # One fetched 45 s before the end still has nearly its full life.
    assert page.evaluate("([s, t]) => linkLasts(s, t)", [{"expires": now + 555}, track]) is True


def test_where_volume_is_fixed_the_stream_is_asked_to_carry_the_gain(page):
    open_library(page)
    url = page.evaluate(
        "player.volumeAdjustable = false;"
        "streamUrlFor('Loud Band/Singles/Loud.opus').then(s => s.url)"
    )
    assert url.endswith("&norm=1")  # -6 dB ReplayGain, and the page cannot apply it
    url = page.evaluate(
        "player.volumeAdjustable = true;"
        "streamUrlFor('Loud Band/Singles/Loud.opus').then(s => s.url)"
    )
    assert "norm" not in url  # the slider does it here


def test_a_downloaded_track_plays_and_seeks_without_a_network(page):
    open_library(page)
    assert page.evaluate("offline.supported") is True
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    # The page must be controlled by the worker before it can answer offline.
    if not page.evaluate("!!navigator.serviceWorker.controller"):
        page.reload()
        page.wait_for_function(
            "typeof switchView === 'function' && !!navigator.serviceWorker.controller"
        )
    page.evaluate(
        "downloadTrack({path: 'Loud Band/Singles/Loud.opus', title: 'Loud', artist: 'Loud Band',"
        " duration: 12})"
    )
    assert page.evaluate("isDownloaded('Loud Band/Singles/Loud.opus')") is True

    page.context.set_offline(True)
    try:
        # A piece of the file, the way <audio> asks for it: an honest 206.
        part = page.evaluate(
            "fetch('/api/stream?path=' + encodeURIComponent('Loud Band/Singles/Loud.opus'),"
            " {headers: {Range: 'bytes=0-9'}}).then(async r => [r.status,"
            " r.headers.get('Content-Range'), (await r.arrayBuffer()).byteLength])"
        )
        assert part[0] == 206 and part[1].startswith("bytes 0-9/") and part[2] == 10

        page.evaluate(
            "playQueue([{path: 'Loud Band/Singles/Loud.opus', title: 'Loud', artist: 'Loud Band',"
            " duration: 12}], 0)"
        )
        page.wait_for_function("!player.audio.paused && player.audio.currentTime > 0.3")
        page.evaluate("player.audio.currentTime = 8")
        page.wait_for_function("player.audio.currentTime > 8.2 && !player.audio.paused")
        assert page.evaluate("player.audio.error") is None
    finally:
        page.context.set_offline(False)

    page.evaluate("removeAllDownloads()")
    assert page.evaluate("isDownloaded('Loud Band/Singles/Loud.opus')") is False


def test_a_play_heard_offline_is_sent_when_the_network_returns(page, server):
    from adder import db

    open_library(page)
    before = db.db_query("SELECT COUNT(*) AS n FROM plays")[0]["n"]
    page.evaluate(
        "localStorage.setItem('pendingPlays', JSON.stringify([{path: 'Loud Band/Singles/Loud.opus',"
        " played_seconds: 12, duration: 12, skipped: false, source: 'player', mode: 'manual'}]))"
    )
    page.evaluate("flushPendingPlays()")
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0")
    assert db.db_query("SELECT COUNT(*) AS n FROM plays")[0]["n"] == before + 1


def test_the_sleep_timer_counts_down_and_stops_playback(page):
    open_library(page)
    row(page, "Loud").get_by_role("button", name="Играть").click()
    page.wait_for_function("!player.audio.paused")

    page.get_by_role("button", name="Таймер сна").click()
    page.get_by_role("menuitem", name="15 минут").click()
    assert page.locator("#playerSleepLeft").inner_text() == "15"

    # Five seconds before the end the volume is halfway down...
    page.evaluate("player.sleep.until = Date.now() + 5000; fadeTick()")
    assert page.evaluate("player.fadeLevel") == pytest.approx(0.5, abs=0.1)
    # ...and at the end playback stops and the timer clears itself.
    page.evaluate("player.sleep.until = Date.now() - 1; sleepTick()")
    assert page.evaluate("player.audio.paused") is True
    assert page.locator("#playerSleepLeft").is_hidden()


def test_retry_button_appears_only_with_failures(page):
    from adder import db

    page.evaluate("switchView('viewAdd')")
    page.evaluate("tasks()")
    assert page.locator("#retryFailed").is_hidden()

    db.db_exec(
        "INSERT INTO tasks(url, status, error, error_type) "
        "VALUES('https://www.youtube.com/watch?v=ui-failed', 'error', 'x', 'network_error')"
    )
    page.evaluate("tasks()")
    page.wait_for_selector("#retryFailed:not([hidden])")
    db.db_exec("DELETE FROM tasks WHERE url = 'https://www.youtube.com/watch?v=ui-failed'")


def test_library_health_card(page):
    page.evaluate("switchView('viewService')")

    page.wait_for_selector("#libraryHealth .fact")
    text = page.locator("#libraryHealth").inner_text()

    assert "Без обложки" in text
    assert page.get_by_role("button", name="Найти обложки").is_visible()


def test_album_search_lists_choices_and_downloads_the_chosen_one(page):
    albums = [
        {
            "id": "1",
            "title": "OK Computer",
            "artist": "Radiohead",
            "tracks": 12,
            "cover": "",
            "type": "album",
        }
    ]
    imported = []
    page.route("**/api/albums/search*", lambda route: route.fulfill(json=albums))

    def import_album(route):
        imported.append(route.request.post_data_json)
        route.fulfill(
            json={
                "read": 12,
                "queued": 11,
                "unmatched": [{"artist": "Radiohead", "title": "Lucky"}],
            }
        )

    page.route("**/api/import-album", import_album)
    page.evaluate("switchView('viewAdd')")
    page.fill("#searchQuery", "radiohead ok computer")
    page.get_by_role("button", name="Альбом").click()
    page.get_by_role("button", name="Скачать альбом").click()

    page.wait_for_selector("#searchNote:has-text('в очередь 11')")
    assert imported == [{"id": "1"}]
    assert "Lucky" in page.locator("#searchNote").inner_text()


def test_a_duplicate_pair_is_shown_and_can_be_kept_both(page):
    from adder import db

    tid = db.db_exec(
        "INSERT INTO tasks(url, status, result_path, warning, similar_to) VALUES("
        "'https://www.youtube.com/watch?v=ui-dup', 'done', 'Quiet/Singles/Quiet.m4a', "
        "'Похоже на уже имеющийся трек: Loud Band/Singles/Loud.opus', 'Loud Band/Singles/Loud.opus')"
    ).lastrowid
    page.evaluate("switchView('viewAdd')")
    page.evaluate("lastWarned = null; tasks()")

    pair = page.locator("#duplicates .duplicate")
    pair.wait_for()
    assert "Был в фонотеке" in pair.inner_text()
    assert "kbit" not in pair.inner_text()  # facts are in Russian: "кбит/с"

    pair.get_by_role("button", name="Оставить оба").click()
    page.wait_for_selector("#duplicatesHead", state="hidden")
    assert db.db_query("SELECT warning FROM tasks WHERE id = ?", (tid,))[0]["warning"] is None
