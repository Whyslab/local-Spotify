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

# The module's server fixture points every path into its own temp dir; the
# per-test redirection in conftest would pull them out from under it.
OWN_RUNTIME = True

from adder import (  # noqa: E402
    analysis,
    config,
    covers,
    discography,
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
        "WEB_COVERS_DIR": tmp / "web-covers",
        "ARTIST_PHOTOS_DIR": tmp / "artist-photos",
        "ARTIST_IMPORT_FILE": tmp / "artist-import.json",
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
    mp.setattr(similar, "_ask_picture", lambda *a, **k: None)  # фото артиста
    mp.setattr(discography, "start", lambda queue_one: None)  # поиск на YouTube в фоне
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


def row_action(page, title, action):
    """Действия строки фонотеки живут в меню «⋯» (с 30.09.2026)."""
    row(page, title).get_by_role("button", name="Что сделать с треком").click()
    page.get_by_role("menuitem", name=action).click()


# ---------------------------------------------------------------------------


def test_the_library_lists_tracks_and_renders_titles_as_text(page):
    open_library(page)

    titles = page.locator("#library .track-title").all_inner_texts()

    assert {"Loud", "Quiet", XSS_TITLE} <= set(titles)
    assert page.evaluate("window.pwned") is None  # the title never became markup


def test_tags_are_edited_in_place(page, server):
    open_library(page)
    row_action(page, "Quiet", "Изменить теги…")

    form = page.locator("form.edit-tags")
    form.get_by_label("Название").fill("Quiet Storm")
    form.get_by_label("Альбом").fill("Night")
    form.get_by_role("button", name="Сохранить").click()

    page.wait_for_selector("#library .track-title:text-is('Quiet Storm')")
    tags = library.read_tags(server["root"] / "Quiet/Singles/Quiet.m4a")
    assert (tags["title"], tags["album"]) == ("Quiet Storm", "Night")


def test_replaygain_turns_a_loud_track_down_but_not_the_slider(page):
    open_library(page)
    row_action(page, "Loud", "Играть")
    page.wait_for_function("!player.audio.paused && player.audio.currentTime > 0")
    assert page.evaluate("player.audio.error") is None

    volume = page.evaluate("player.audio.volume")
    slider = page.evaluate("document.getElementById('playerVolumeRange').value")

    assert volume == pytest.approx(10 ** (-6 / 20), abs=0.01)  # -6 dB
    assert slider == "100"  # the listener's own level is unchanged


def test_fades_at_the_ends_of_a_track(page):
    open_library(page)
    row_action(page, "Loud", "Играть")
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
    row_action(page, "Loud", "Играть")
    page.wait_for_function("!player.audio.paused && player.audio.duration > 10")
    page.evaluate("setFade(3); player.audio.pause()")

    # The state between two tracks: a new source, nothing known about it yet.
    page.evaluate("player.audio.removeAttribute('src'); player.audio.load(); fadeTick()")
    assert page.evaluate("Number.isNaN(player.audio.duration)")
    assert page.evaluate("player.fadeLevel") == 0


def test_a_fade_is_driven_by_a_fast_timer_only_while_it_runs(page):
    open_library(page)
    row_action(page, "Loud", "Играть")
    page.wait_for_function("!player.audio.paused && player.audio.duration > 10")
    page.evaluate("setFade(3); player.audio.currentTime = 6; fadeTick()")
    assert page.evaluate("fadeTimer") is None  # middle of the track: no fade

    page.evaluate("player.audio.currentTime = player.audio.duration - 2; fadeTick()")
    assert page.evaluate("fadeTimer") is not None

    page.evaluate("player.audio.pause(); fadeTick()")
    assert page.evaluate("fadeTimer") is None


def test_the_lock_screen_shows_the_track_and_its_state(page):
    open_library(page)
    row_action(page, "Loud", "Играть")
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


LOUD = "{path: 'Loud Band/Singles/Loud.opus', title: 'Loud', artist: 'Loud Band', duration: 12}"


def _controlled(page):
    """The offline worker answers only once it controls the page."""
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    if not page.evaluate("!!navigator.serviceWorker.controller"):
        page.reload()
        page.wait_for_function(
            "typeof switchView === 'function' && !!navigator.serviceWorker.controller"
        )


def test_the_page_is_saved_for_offline_on_the_very_first_visit(page):
    open_library(page)
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    page.wait_for_function(
        "caches.open('shell-v1').then(c => c.keys()).then(k => window.shellKeys = k.map(r => new URL(r.url).pathname))"
        " && window.shellKeys && window.shellKeys.includes('/static/player.js')",
        timeout=15000,
    )
    keys = page.evaluate("window.shellKeys")
    assert "/" in keys and "/static/offline.js" in keys and "/static/style.css" in keys


def test_an_unreachable_server_skips_to_a_downloaded_track(page):
    open_library(page)
    _controlled(page)
    page.evaluate(f"downloadTrack({LOUD})")
    # Online as far as the phone knows, but the server does not answer.
    page.evaluate(
        "const real = window.fetch; window.fetch = (url, opts) =>"
        " String(url).includes('Quiet.m4a') ? Promise.reject(new TypeError('Load failed')) : real(url, opts)"
    )
    page.evaluate(
        "playQueue([{path: 'Quiet/Singles/Quiet.m4a', title: 'Quiet', artist: 'Quiet', duration: 12},"
        f" {LOUD}], 0)"
    )
    page.wait_for_function("player.index === 1 && !player.audio.paused")
    page.evaluate("removeAllDownloads()")


def test_a_track_that_never_started_is_not_logged_as_played(page):
    open_library(page)
    posts = []
    page.on("request", lambda r: posts.append(r) if r.url.endswith("/api/plays") else None)
    page.evaluate(
        "player.queue = [{path: 'X/Singles/x.m4a', title: 'x'}]; player.index = 0;"
        "player.started = false; player.reported = false; reportPlay(false)"
    )
    page.wait_for_timeout(300)
    assert posts == []


def test_a_failed_load_does_not_leave_the_previous_track_playing(page):
    open_library(page)
    page.evaluate(f"playQueue([{LOUD}, {{path: 'Nope/Singles/nope.m4a', title: 'Nope'}}], 0)")
    page.wait_for_function("!player.audio.paused")
    page.evaluate("nextTrack()")
    page.wait_for_function("player.index === 1 && !player.audio.getAttribute('src')")
    assert page.evaluate("player.audio.paused") is True


def test_removing_a_download_drops_its_prepared_next_link(page):
    open_library(page)
    _controlled(page)
    page.evaluate(f"downloadTrack({LOUD})")
    page.evaluate(
        "nextStream = {path: 'Loud Band/Singles/Loud.opus', url: '/api/stream?sig=offline',"
        " gain: null, expires: Date.now() / 1000 + 9999}"
    )
    page.evaluate("removeDownloaded('Loud Band/Singles/Loud.opus')")
    assert page.evaluate("nextStream") is None


def test_remove_all_during_a_download_keeps_it_removed(page):
    open_library(page)
    _controlled(page)
    page.evaluate(f"window.pending = downloadTrack({LOUD}); removeAllDownloads()")
    page.evaluate("pending.catch(() => {})")
    assert page.evaluate("isDownloaded('Loud Band/Singles/Loud.opus')") is False
    keys = page.evaluate("caches.open('offline-audio-v1').then(c => c.keys()).then(k => k.length)")
    assert keys == 0


def test_a_download_brings_its_covers_and_a_library_search_is_not_kept(page):
    open_library(page)
    _controlled(page)
    # Give it words, so there is something to keep besides the audio.
    page.evaluate(
        "fetch('/api/lyrics/custom', {method: 'POST', headers: {...headers(),"
        " 'Content-Type': 'application/json'}, body: JSON.stringify({path:"
        " 'Loud Band/Singles/Loud.opus', text: 'la la la'})}).then(r => r.status)"
    )
    page.evaluate(f"downloadTrack({LOUD})")
    # Kept: exactly what the server has for it, cover sizes and lyrics alike.
    kept, served = page.evaluate(
        "(async () => {"
        " const path = 'Loud Band/Singles/Loud.opus';"
        " const urls = [96, 300, 600].map(s => coverKey(path, s)).concat([lyricsKey(path)]);"
        " const served = [];"
        " for (const u of urls) { const r = await fetch(u + '&fresh=1', {headers: headers()});"
        "   if (r.ok) served.push(new URL(u, location.href).href); }"
        " const keys = await (await caches.open('offline-covers-v1')).keys();"
        " return [keys.map(r => r.url).sort(), served.sort()]; })()"
    )
    assert kept == served and any("lyrics" in u for u in kept)
    page.evaluate("fetch('/api/library?q=lou', {headers: headers()})")
    kept = page.evaluate("caches.open('api-v1').then(c => c.keys()).then(k => k.map(r => r.url))")
    assert not any("q=lou" in u for u in kept)
    page.evaluate("removeAllDownloads()")


def test_a_download_whose_file_changed_on_the_server_is_fetched_again(page):
    open_library(page)
    _controlled(page)
    page.evaluate(f"downloadTrack({LOUD})")
    # As if the library file had been replaced since (a new version, Opus).
    page.evaluate(
        "caches.open('offline-meta-v1').then(async c => {"
        " const key = '/offline/meta?path=' + encodeURIComponent('Loud Band/Singles/Loud.opus');"
        " const meta = await (await c.match(key)).json(); meta.size = 1;"
        " await c.put(key, new Response(JSON.stringify(meta))); })"
        ".then(() => localStorage.removeItem('offlineChecked'))"
    )
    page.evaluate("reconcileDownloads()")
    size = page.evaluate(
        "caches.open('offline-meta-v1').then(async c => (await (await c.match('/offline/meta?path='"
        " + encodeURIComponent('Loud Band/Singles/Loud.opus'))).json()).size)"
    )
    assert size > 1
    page.evaluate("removeAllDownloads()")


def test_the_sleep_timer_counts_down_and_stops_playback(page):
    open_library(page)
    row_action(page, "Loud", "Играть")
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
    page.locator("#viewAdd").get_by_role("button", name="Альбом", exact=True).click()
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


# --- «⋯» у строк фонотеки (30.09.2026); подборки — свой раздел снова (01.10) --


def test_all_playlists_are_a_tab_with_a_grid(page, server):
    (server["root"] / "Сетка.m3u").write_text("Quiet/Singles/Quiet.m4a\n", encoding="utf-8")
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("playlists()")
    page.wait_for_function("knownPlaylists.some(p => p.name === 'Сетка')")
    page.locator('.tab[data-view="viewPlaylists"]').click()
    page.wait_for_function("activeView === 'viewPlaylists'")
    card = page.locator("#playlistsBody .o-card").filter(has_text="Сетка")
    card.click()
    page.wait_for_function("activeView === 'viewPlaylist'")
    assert page.locator("#playlistName").inner_text() == "Сетка"
    # «Назад» возвращает к сетке — шагом истории, как жест на телефоне.
    page.go_back()
    page.wait_for_function("activeView === 'viewPlaylists'")


def test_a_library_row_has_one_menu_button_with_four_actions(page):
    open_library(page)
    buttons = row(page, "Quiet").get_by_role("button")
    assert buttons.count() == 1
    buttons.first.click()
    items = page.get_by_role("menuitem").all_inner_texts()
    assert items == ["Играть", "В подборку…", "Изменить теги…", "Удалить…"]
    page.keyboard.press("Escape")
    assert page.get_by_role("menuitem").count() == 0


def test_the_rail_plus_creates_a_playlist_and_opens_it(page, server):
    page.locator("#railNewPlaylist").click()
    field = page.get_by_role("dialog", name="Новая подборка").get_by_role("textbox")
    field.fill("Проверка плюса")
    field.press("Enter")
    page.wait_for_function("activeView === 'viewPlaylist'")
    assert page.locator("#playlistName").inner_text() == "Проверка плюса"
    assert (server["root"] / "Проверка плюса.m3u").exists()
    page.wait_for_selector("#railPlaylists .rail-item:has-text('Проверка плюса')")
    # «Назад» возвращает туда, откуда пришли, а не на пропавший раздел.
    page.locator("#playlistMore").click()
    page.get_by_role("menuitem", name="Назад").click()
    page.wait_for_function("activeView === 'viewHome'")


def test_an_empty_name_says_so_instead_of_doing_nothing(page):
    page.locator("#railNewPlaylist").click()
    dialog = page.get_by_role("dialog", name="Новая подборка")
    dialog.get_by_role("button", name="Создать").click()
    assert "Впиши название" in dialog.inner_text()


def test_on_a_phone_the_four_tabs_are_home_library_playlists_search(page):
    page.set_viewport_size({"width": 390, "height": 844})
    tabs = page.locator(".tabbar .tab").all_inner_texts()
    assert [t.strip() for t in tabs] == ["Главная", "Фонотека", "Подборки", "Поиск"]
    # «Добавить» — плюс в заголовке, «Служба» — по нажатию на состояние.
    page.locator("#topAdd").click()
    page.wait_for_function("activeView === 'viewAdd'")
    page.evaluate("switchView('viewHome')")
    page.locator("#statusPill").click()
    page.wait_for_function("activeView === 'viewService'")


def test_playlist_rows_have_no_dots_but_still_drag(page, server):
    (server["root"] / "Тащить.m3u").write_text(
        "Quiet/Singles/Quiet.m4a\nLoud Band/Singles/Loud.opus\n", encoding="utf-8"
    )
    page.evaluate("openPlaylist('Тащить')")
    page.wait_for_selector("#playlistTracks .playlist-track")
    assert page.locator("#playlistTracks .handle").count() == 0
    assert (
        page.locator("#playlistTracks .playlist-track").first.get_attribute("draggable") == "true"
    )


# --- Шапка подборки: «Слушать», «Перемешать» и «⋯» (01.10.2026) ------------


def test_the_playlist_head_keeps_two_buttons_and_a_menu(page, server):
    (server["root"] / "Шапка.m3u").write_text("Quiet/Singles/Quiet.m4a\n", encoding="utf-8")
    page.evaluate("openPlaylist('Шапка')")
    page.wait_for_function("activeView === 'viewPlaylist'")
    visible = page.locator(".playlist-meta .row button:visible").all_inner_texts()
    assert visible == ["Слушать", "Перемешать", "⋯"]
    page.locator("#playlistMore").click()
    items = page.get_by_role("menuitem").all_inner_texts()
    assert items[0] == "Обложка…" and "Переименовать…" in items and items[-1] == "Удалить подборку…"
    # Стрелки ходят по пунктам, Escape закрывает.
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.textContent") == items[1]
    page.keyboard.press("Escape")
    assert page.get_by_role("menuitem").count() == 0


def test_a_playlist_is_deleted_from_its_menu(page, server):
    target = server["root"] / "Удалить меня.m3u"
    target.write_text("Quiet/Singles/Quiet.m4a\n", encoding="utf-8")
    page.evaluate("openPlaylist('Удалить меня')")
    page.wait_for_function("activeView === 'viewPlaylist'")
    page.locator("#playlistMore").click()
    page.get_by_role("menuitem", name="Удалить подборку…").click()
    page.locator("#playlistEdit .confirm").get_by_role("button", name="Удалить").click()
    page.wait_for_function("activeView !== 'viewPlaylist'")
    assert not target.exists()


def test_the_stall_watchdog_gives_up_after_three_tries(page):
    """Новая ссылка сама вызывает emptied — счётчик попыток не должен от этого
    обнуляться, иначе сторож перезапрашивал бы трек вечно."""
    calls = page.evaluate(
        """async () => {
            let n = 0;
            reloadCurrentSource = async () => { n += 1; player.audio.dispatchEvent(new Event('emptied')); };
            Object.defineProperty(player.audio, 'paused', { get: () => false, configurable: true });
            Object.defineProperty(player.audio, 'readyState', { get: () => 1, configurable: true });
            const real = window.setTimeout;
            window.setTimeout = (fn, ms) => real(fn, ms === STALL_MS ? 5 : ms);
            for (let i = 0; i < 6; i++) {
                watchForStall();
                await new Promise(r => real(r, 30));
            }
            window.setTimeout = real;
            return n;
        }"""
    )
    assert calls == 3


# --- «Обложка»: большой плеер на телефоне, цвет, «откуда играет» (01.10.2026) --


def _play_on_phone(page):
    page.set_viewport_size({"width": 390, "height": 844})
    open_library(page)
    row_action(page, "Loud", "Играть")
    page.wait_for_function("!player.audio.paused")


def test_the_phone_player_opens_full_screen_and_back_closes_it(page):
    _play_on_phone(page)
    page.locator("#playerArt").click()
    page.wait_for_function("document.querySelector('.app').classList.contains('player-open')")
    assert page.locator("#player").get_attribute("role") == "dialog"
    # Текст песни переезжает внутрь большого плеера…
    page.locator("#playerLyricsButton").click()
    assert page.evaluate("document.getElementById('viewLyrics').parentElement.id") == "playerPanel"
    # …а «назад» сворачивает плеер и возвращает текст на место, скрытым.
    page.go_back()
    page.wait_for_function("!document.querySelector('.app').classList.contains('player-open')")
    assert page.evaluate("document.getElementById('viewLyrics').parentElement.id") == "views"
    assert page.evaluate("document.getElementById('viewLyrics').hidden") is True
    assert page.evaluate("activeView") == "viewLibrary"


def test_escape_closes_a_menu_in_the_big_player_but_not_the_player(page):
    _play_on_phone(page)
    page.evaluate("expandPlayer()")
    page.locator("#playerShuffle").click()
    assert page.locator(".shuffle-menu").count() == 1
    page.keyboard.press("Escape")
    assert page.locator(".shuffle-menu").count() == 0
    assert page.evaluate("document.querySelector('.app').classList.contains('player-open')")
    page.keyboard.press("Escape")
    assert not page.evaluate("document.querySelector('.app').classList.contains('player-open')")


def test_the_cover_colour_can_be_switched_off(page):
    page.evaluate("switchView('viewService')")
    switch = page.locator("#coverTintSwitch")
    assert switch.is_checked()
    switch.click()
    assert page.evaluate("localStorage.getItem('coverTint')") == "0"
    assert not page.evaluate("document.querySelector('.app').classList.contains('is-tinted')")
    page.reload()
    page.wait_for_function("typeof switchView === 'function'")
    page.evaluate("switchView('viewService')")
    assert not page.locator("#coverTintSwitch").is_checked()


def test_a_shuffled_playlist_shows_its_track_in_the_playlist(page, server):
    (server["root"] / "Откуда.m3u").write_text(
        "Quiet/Singles/Quiet.m4a\nLoud Band/Singles/Loud.opus\n", encoding="utf-8"
    )
    page.evaluate("openPlaylist('Откуда')")
    page.wait_for_selector("#playlistTracks .playlist-track")
    # «Перемешать» собирает очередь на сервере — она всё равно из подборки.
    page.evaluate("loadShuffle('plain', 'Откуда', 'playlistNote')")
    page.wait_for_function("player.queueSource && player.queueSource.name === 'Откуда'")
    page.evaluate("switchView('viewHome')")

    page.evaluate(
        "revealTrack(player.queue.find(t => t.path === 'Quiet/Singles/Quiet.m4a' && !t.outside))"
    )
    page.wait_for_function("activeView === 'viewPlaylist'")
    found = page.locator("#playlistTracks .is-found")
    assert found.count() == 1
    assert found.get_attribute("data-track-path") == "Quiet/Singles/Quiet.m4a"

    # Подмешанный не из подборки — его место в фонотеке.
    page.evaluate("revealTrack({path: 'Evil/Singles/evil.m4a', title: 'evil', outside: true})")
    page.wait_for_function("activeView === 'viewLibrary'")


def test_smart_shuffle_keeps_the_playlist_as_the_source(page, server):
    (server["root"] / "Умно.m3u").write_text(
        "Quiet/Singles/Quiet.m4a\nLoud Band/Singles/Loud.opus\n", encoding="utf-8"
    )
    page.evaluate("openPlaylist('Умно')")
    page.wait_for_selector("#playlistTracks .playlist-track")
    page.evaluate("playPlaylist()")
    page.wait_for_function("player.queueSource && player.queueSource.name === 'Умно'")
    page.evaluate("setShuffle('smart')")
    page.wait_for_function("player.queueMode === 'smart'")
    assert page.evaluate("player.queueSource && player.queueSource.name") == "Умно"


def test_a_track_twice_in_a_playlist_is_shown_at_the_copy_that_plays(page, server):
    (server["root"] / "Дважды.m3u").write_text(
        "Quiet/Singles/Quiet.m4a\nLoud Band/Singles/Loud.opus\nQuiet/Singles/Quiet.m4a\n",
        encoding="utf-8",
    )
    page.evaluate("openPlaylist('Дважды')")
    page.wait_for_selector("#playlistTracks .playlist-track")
    # Третья строка — вторая копия того же файла.
    page.evaluate(
        "playQueue(player.playlist.entries, 2, 'manual', {kind: 'playlist', name: 'Дважды'})"
    )
    page.evaluate("switchView('viewHome')")
    page.evaluate("revealTrack(player.queue[2])")
    page.wait_for_function("activeView === 'viewPlaylist'")
    assert page.locator("#playlistTracks .is-found").get_attribute("data-index") == "2"


# --- Экраны «Обложки»: артисты, альбомы, поиск, главная (01.10.2026) ---------


def test_artists_open_an_artist_page_and_back_returns(page):
    page.evaluate("openLibrary('artists')")
    page.wait_for_selector("#library .o-artist-row")
    names = page.locator("#library .o-artist-row .o-row-title").all_inner_texts()
    assert {"Loud Band", "Quiet"} <= set(names)
    page.locator("#library .o-artist-row").filter(has_text="Loud Band").click()
    page.wait_for_function("activeView === 'viewArtist'")
    assert page.locator("#artistBody h1").inner_text() == "Loud Band"
    assert page.locator("#artistBody .track-title").all_inner_texts() == ["Loud"]
    # У артиста без альбомов не должно остаться пустого места словом «null».
    assert "null" not in page.locator("#artistBody").inner_text()
    page.go_back()
    page.wait_for_function("activeView === 'viewLibrary' && libraryMode === 'artists'")


def test_an_album_page_lists_its_tracks_in_order(page, server):
    from adder import enrich as enrich_module

    for title, number in (("One", 1), ("Two", 2)):
        path = server["root"] / f"Duo/Singles/{title}.opus"
        _track(server["root"], f"Duo/Singles/{title}.opus", "Duo", title, 2, gain=None)
        ingest.write_tags(
            path,
            enrich_module.TrackInfo(album="Pair", artists=["Duo"], track_number=number),
            title,
            None,
            None,
        )
    library.invalidate_library_index()
    page.evaluate("labTracks = null")
    page.evaluate("openLibrary('albums')")
    page.wait_for_selector("#library .o-grid .o-card")
    page.locator("#library .o-card").filter(has_text="Pair").click()
    page.wait_for_function("activeView === 'viewAlbum'")
    assert page.locator("#albumBody .o-col-title").inner_text() == "Pair"
    assert page.locator("#albumBody .track-title").all_inner_texts() == ["One", "Two"]


def test_search_finds_artists_and_tracks_in_the_library(page):
    page.evaluate("switchView('viewSearch')")
    page.locator("#searchEverywhere").fill("loud")
    page.wait_for_selector("#searchBody .track-title")
    assert "Loud" in page.locator("#searchBody .track-title").all_inner_texts()
    assert page.locator("#searchBody .o-card.is-artist").filter(has_text="Loud Band").count() == 1
    # Чего нет в фонотеке — тем же запросом на YouTube, во «Добавить».
    assert page.locator("#searchBody .o-yt-ask").count() == 1


def test_shuffle_offers_plain_and_smart(page):
    open_library(page)
    page.locator("#libraryShuffle").click()
    items = page.get_by_role("menuitem").all_inner_texts()
    assert [i.split("\n")[0] for i in items] == ["Обычное", "Умное"]
    page.keyboard.press("Escape")
    assert page.get_by_role("menuitem").count() == 0


def test_back_after_switching_tabs_returns_to_that_tab(page, server):
    (server["root"] / "Вкладки.m3u").write_text("Quiet/Singles/Quiet.m4a\n", encoding="utf-8")
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("playlists()")
    page.wait_for_function("knownPlaylists.some(p => p.name === 'Вкладки')")
    page.evaluate("openLibrary('artists')")
    page.wait_for_selector("#library .o-artist-row")
    # Шаг вглубь и обратно: теперь у текущей записи истории есть свой раздел…
    page.locator("#library .o-artist-row").first.click()
    page.wait_for_function("activeView === 'viewArtist'")
    page.go_back()
    page.wait_for_function("activeView === 'viewLibrary'")
    # …и смена вкладки обязана его переписать.
    page.locator('.tab[data-view="viewPlaylists"]').click()
    page.locator("#playlistsBody .o-card").filter(has_text="Вкладки").click()
    page.wait_for_function("activeView === 'viewPlaylist'")
    page.go_back()
    page.wait_for_function("activeView !== 'viewPlaylist'")
    assert page.evaluate("activeView") == "viewPlaylists"


def test_a_new_playlist_is_made_from_the_playlists_tab_on_a_phone(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('.tab[data-view="viewPlaylists"]').click()
    page.wait_for_function("activeView === 'viewPlaylists'")
    page.locator("#topAdd").click()
    assert page.get_by_role("dialog", name="Новая подборка").count() == 1


def test_a_deep_page_opens_at_its_top(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("openLibrary('tracks')")
    page.wait_for_selector("#library .track")
    page.evaluate("document.body.style.minHeight = '5000px'; window.scrollTo(0, 1500)")
    page.evaluate("openArtistPage('Quiet')")
    page.wait_for_function("activeView === 'viewArtist'")
    page.wait_for_function("window.scrollY === 0")


def test_deep_pages_are_no_wider_than_the_phone(page, server):
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("openArtistPage('Quiet')")
    page.wait_for_function("activeView === 'viewArtist'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(
        "labIndex().then(rows => openAlbumPage(groupIntoAlbums(rows).find(g => g.tracks.length)))"
    )
    page.wait_for_function("activeView === 'viewAlbum'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


# --- правки 02.10.2026 ------------------------------------------------------


def _playlist(server, page, name, *paths):
    (server["root"] / f"{name}.m3u").write_text("\n".join(paths) + "\n", encoding="utf-8")
    page.evaluate("playlists()")
    page.wait_for_function(f"knownPlaylists.some(p => p.name === {name!r})")


def test_a_playlist_is_lit_in_the_rail_only_while_it_is_open(page, server):
    page.set_viewport_size({"width": 1300, "height": 800})
    _playlist(server, page, "Рельса", "Quiet/Singles/Quiet.m4a")
    lit = "[...document.querySelectorAll('.rail-item.is-active')].map(i => i.title)"
    page.evaluate("openPlaylist('Рельса')")
    page.wait_for_function("activeView === 'viewPlaylist'")
    assert page.evaluate(lit) == ["Рельса"]
    # Ушли на главную — подборка в рельсе гаснет (раньше горела рядом с ней).
    page.evaluate("switchView('viewHome')")
    assert page.evaluate(lit) == []
    page.evaluate("renderRail(knownPlaylists)")  # опрос раз в 30 с не зажигает её снова
    assert page.evaluate(lit) == []


@pytest.mark.parametrize("size", [(1266, 603), (390, 844)], ids=["laptop-150%", "phone"])
def test_a_track_menu_stays_inside_a_low_window(page, server, size):
    """Браузер в масштабе 150 %: около 600 точек в высоту, полоса плеера внизу."""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    _playlist(server, page, "Низкое", *["Quiet/Singles/Quiet.m4a"] * 12)
    page.evaluate("openPlaylist('Низкое')")
    page.wait_for_selector("#viewPlaylist .track")
    page.locator("#viewPlaylist .track .track-info").first.click()  # появляется полоса плеера
    page.wait_for_function("!document.getElementById('player').hidden")
    for index in (0, 5, 11):
        target = page.locator("#viewPlaylist .track").nth(index)
        target.scroll_into_view_if_needed()
        target.get_by_role("button", name="Что сделать с треком").click()
        box = page.evaluate(
            "(() => { const b = document.querySelector('.row-menu').getBoundingClientRect();"
            " const floor = Math.min(innerHeight, ...[...document.querySelectorAll('#player, .tabbar')]"
            "   .map(n => n.getBoundingClientRect()).filter(r => r.height).map(r => r.top));"
            " return [b.top, b.bottom, floor]; })()"
        )
        assert box[0] >= 0, f"строка {index}: меню уходит за верх окна ({box})"
        assert box[1] <= box[2], f"строка {index}: меню уходит под полосу внизу ({box})"
        page.keyboard.press("Escape")


def test_the_big_player_fits_a_low_window_and_play_is_a_white_circle(page):
    page.set_viewport_size({"width": 1266, "height": 603})
    open_library(page)
    row(page, "Loud").locator(".track-info").click()
    page.wait_for_function("!document.getElementById('player').hidden")
    page.evaluate("expandPlayer('art')")
    page.wait_for_function("document.querySelector('.app').classList.contains('player-open')")
    page.wait_for_timeout(500)  # лист выезжает снизу
    art = page.locator("#playerArt").bounding_box()
    assert art["y"] >= 0, f"обложка уходит за верх окна: {art}"
    assert abs(art["width"] - art["height"]) < 2, f"обложка не квадратная: {art}"
    play = page.evaluate(
        "getComputedStyle(document.querySelector('.player-controls .primary-round')).backgroundColor"
    )
    assert play == "rgb(255, 255, 255)"


def test_new_finds_on_every_visit_home(page, monkeypatch):
    from adder import shelves

    shown = iter(range(1000))

    def external(rows, want=12, seed=None):
        n = next(shown)
        return [{"artist": f"Артист {n}", "title": f"Находка {n}", "cover": ""}]

    monkeypatch.setattr(shelves, "external", external)
    titles = "[...document.querySelectorAll('.o-card:has(.is-find) .o-card-title')].map(t => t.textContent)"
    # Следующий набор страница готовит заранее — тот, что заготовлен до
    # подмены, пропускаем лишним заходом.
    page.evaluate("switchView('viewLibrary'); switchView('viewHome')")
    page.evaluate("switchView('viewLibrary'); switchView('viewHome')")
    page.wait_for_function(f"{titles}.length > 0")
    first = page.evaluate(titles)
    page.evaluate("switchView('viewLibrary')")
    page.evaluate("switchView('viewHome')")
    page.wait_for_function(f"{titles}[0] !== {first[0]!r}")
    # Фоновый опрос главной набор не меняет — под рукой полка не прыгает.
    second = page.evaluate(titles)
    page.evaluate("refresh(true)")
    page.wait_for_timeout(300)
    assert page.evaluate(titles) == second


def test_an_artist_page_shows_its_own_header(page):
    from adder import artist_photos

    artist_photos.store("Quiet", _tiny_png())
    try:
        page.evaluate("openArtistPage('Quiet')")
        page.wait_for_selector("#artistBody .o-hero .o-cover img")
        page.locator("#artistBody .o-more-round").click()
        # Меню открывается после ответа «своя ли шапка» — ждём его.
        page.get_by_role("menuitem").first.wait_for()
        assert page.get_by_role("menuitem").all_inner_texts() == [
            "Скачать недостающее…",
            "Другая шапка…",
            "Вернуть фото из Deezer",
        ]
    finally:
        artist_photos.delete("Quiet")


def _tiny_png() -> bytes:
    import struct
    import zlib

    def chunk(kind, body):
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def test_no_lyrics_says_so_in_large_letters(page):
    # На телефоне текст открывается кнопкой; на ноутбуке он и так справа.
    page.set_viewport_size({"width": 390, "height": 844})
    open_library(page)
    row(page, "Loud").locator(".track-info").click()
    page.wait_for_function("!document.getElementById('player').hidden")
    page.evaluate("expandPlayer('art')")
    page.locator("#playerLyricsButton").click()
    empty = page.locator(".lyric-empty")
    empty.wait_for()
    assert empty.inner_text() == "Извините, но тут буквально ничего"
    size = page.evaluate(
        "parseFloat(getComputedStyle(document.querySelector('.lyric-empty')).fontSize)"
    )
    assert size >= 26


# --- Артист целиком ----------------------------------------------------------


@pytest.fixture
def fake_deezer_artist(monkeypatch):
    artist = {"id": "77", "name": "Quiet", "picture": "", "fans": 12, "albums": 2}
    disco = {
        "artist": {"id": "77", "name": "Quiet"},
        "hidden": 2,
        "releases": [
            {
                "id": "1",
                "title": "Тишина",
                "kind": "album",
                "year": "2020",
                "cover": "",
                "tracks": [
                    {"artist": "Quiet", "title": "Quiet", "duration": 12, "have": True},
                    {"artist": "Quiet", "title": "Шёпот", "duration": 100, "have": False},
                ],
            },
            {
                "id": "2",
                "title": "Шорох",
                "kind": "single",
                "year": "2021",
                "cover": "",
                "tracks": [{"artist": "Quiet", "title": "Шорох", "duration": 90, "have": False}],
            },
        ],
    }
    monkeypatch.setattr(discography, "search_artists", lambda q, limit=8: [artist])
    monkeypatch.setattr(discography, "find_artist", lambda name: artist)
    monkeypatch.setattr(discography, "discography", lambda aid, rows: disco)
    yield
    discography.clear_finished()
    runtime.ARTIST_IMPORT_FILE.unlink(missing_ok=True)


def test_an_artist_is_downloaded_from_a_ticked_list(page, fake_deezer_artist):
    page.evaluate("switchView('viewAdd')")
    page.locator("#artistQuery").fill("quiet")
    page.locator("#artistImport button.primary").click()
    page.locator(".disco-choice").filter(has_text="Quiet").click()
    page.wait_for_selector(".disco-track")
    # Что уже есть — не отметить; кнопка ждёт выбора.
    assert page.locator(".disco-track.is-have input").is_disabled()
    take = page.locator(".disco-actions button")
    assert take.is_disabled()
    page.get_by_role("button", name="Отметить все").click()
    assert take.inner_text() == "Скачать 2 трека"
    take.click()
    page.wait_for_selector("#artistImportStatus:not([hidden])")
    assert "Ищу на YouTube: 0 из 2" in page.locator("#artistImportStatus").inner_text()
    titles = [i["title"] for i in discography._load()]
    assert titles == ["Шёпот", "Шорох"]
    assert [i["album"] for i in discography._load()] == ["1", "2"]


def test_the_artist_page_offers_what_is_missing(page, fake_deezer_artist):
    page.evaluate("openArtistPage('Quiet')")
    page.wait_for_function("activeView === 'viewArtist'")
    page.locator("#artistBody .o-more-round").click()
    page.get_by_role("menuitem", name="Скачать недостающее…").click()
    page.wait_for_function("activeView === 'viewAdd'")
    page.wait_for_selector(".disco-track")
    assert page.locator("#artistQuery").input_value() == "Quiet"
