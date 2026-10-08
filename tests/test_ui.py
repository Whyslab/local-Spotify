"""The panel and player in a real browser (Playwright + Chromium).

A real service runs in a thread against a temporary library of a few short
tracks, with every outside service stubbed; Chromium opens the page the way a
person would. What the Python tests cannot see - that a button exists, that a
row turns into a form, what the <audio> element's volume actually is - is
checked here.

Needs the browser: `python -m playwright install chromium` (CI does this).
"""

from __future__ import annotations

import importlib.util
import json
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
    mp.setattr(lyrics, "candidates", lambda *a, **k: [])  # «Найти текст»
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
    context.add_init_script(CSP_WATCH)
    page = context.new_page()
    page.wait_for_function = csp_safe_wait(page)
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(server["url"] + "/")
    page.wait_for_function("typeof switchView === 'function'")
    yield page
    violations = page.evaluate("window.__csp || []")
    context.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"
    # The page runs under its real Content-Security-Policy: anything it blocks
    # here would be broken for the user too.
    assert violations == [], f"blocked by the page's CSP: {violations}"


def csp_safe_wait(page):
    """page.wait_for_function that works under the page's real CSP.

    Playwright checks a string predicate with eval inside the page, which the
    Content-Security-Policy (no 'unsafe-eval') refuses. page.evaluate goes
    through the debugging protocol and is not subject to it, so the predicate
    is polled with that instead.
    """

    def wait_for_function(expression, arg=None, timeout=None, polling=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 30000) / 1000
        while True:
            value = page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
            if value:
                return value
            if time.monotonic() > deadline:
                raise TimeoutError(f"wait_for_function timed out: {expression}")
            page.wait_for_timeout(20)

    return wait_for_function


# Every test's page records what its Content-Security-Policy refused.
CSP_WATCH = """
window.__csp = [];
document.addEventListener("securitypolicyviolation", (e) =>
    window.__csp.push(e.effectiveDirective + " " + e.blockedURI));
"""


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


def test_app_ready_is_marked_once_when_the_first_screen_has_its_data(page):
    """The "ready" the speed targets are measured to (plan AC 14): the first
    screen drawn with what came from the API - not the bare page."""
    page.wait_for_function("performance.getEntriesByName('app-ready').length === 1")
    assert page.locator("#homeBody > *").count() > 0
    open_library(page)
    page.evaluate("switchView('viewHome')")
    assert page.evaluate("performance.getEntriesByName('app-ready').length") == 1


def test_deezer_images_pass_the_csp_and_a_foreign_host_does_not(page):
    """Album search, artist photos and releases set Deezer CDN addresses straight."""
    page.route(
        "https://*.dzcdn.net/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=b""),
    )
    deezer = "https://e-cdns-images.dzcdn.net/images/cover/1a2b/250x250-000000-80-0-0.jpg"
    page.evaluate(
        """(src) => {
            const row = albumRow({ title: "A", artist: "B", cover: src, tracks: 3, id: 1 },
                                 document.createElement("p"));
            document.body.appendChild(row);
            row.scrollIntoView();  // the image is loading="lazy"
        }""",
        deezer,
    )
    # The image really was requested: otherwise "no violation" proves nothing.
    page.wait_for_function("document.querySelector('img[src*=\"dzcdn\"]').complete")
    assert page.evaluate("window.__csp") == []

    # The control: the same test does see a host outside img-src.
    page.evaluate(
        "() => { const i = new Image(); i.src = 'https://evil.example/x.png';"
        " document.body.appendChild(i); }"
    )
    page.wait_for_function("window.__csp.length > 0")
    assert page.evaluate("window.__csp") == ["img-src https://evil.example/x.png"]
    page.evaluate("window.__csp = []")  # the fixture asserts none are left


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


def _fake_rows(n):
    """A library of n rows as /api/library lists it, with no files behind it."""
    return json.dumps(
        [
            {"path": f"Fake {i}/Singles/Song {i}.m4a", "title": f"Song {i}", "artist": f"Fake {i}",
             "album": f"Song {i}", "albumartist": f"Fake {i}", "duration": 180, "track": 0,
             "year": ""}
            for i in range(n)
        ]
    )  # fmt: skip


def _scroller_of(selector):
    return f"""(() => {{
        let node = document.querySelector({selector!r});
        while (node && !(node.scrollHeight > node.clientHeight
               && /auto|scroll/.test(getComputedStyle(node).overflowY))) node = node.parentElement;
        return node || document.scrollingElement;
    }})()"""


def test_fast_scroll_requests_only_visible_covers(page):
    """Flicking through the list asked for every cover it passed, 600 px
    ahead: a library of two hundred rows queued two hundred pictures. Now a
    row asks only once the list has stopped (50 ms), a row that left the
    screen gives its request up, and no more than four go at once."""
    page.evaluate(
        """async (list) => {
            const c = window.__covers = {asked: 0, inflight: 0, peak: 0, aborted: 0};
            const real = window.fetch;
            const png = await (await real('/static/icon-180.png')).blob();
            window.fetch = (url, options = {}) => {
                const u = String(url);
                if (u.startsWith('/api/library?')) {
                    return Promise.resolve(new Response(list, {headers: {'Content-Type': 'application/json'}}));
                }
                if (!u.startsWith('/api/cover?')) return real(url, options);
                c.asked += 1; c.inflight += 1; c.peak = Math.max(c.peak, c.inflight);
                return new Promise((ok, fail) => {
                    const timer = setTimeout(() => { c.inflight -= 1; ok(new Response(png)); }, 120);
                    if (options.signal) options.signal.addEventListener('abort', () => {
                        clearTimeout(timer); c.inflight -= 1; c.aborted += 1;
                        fail(new DOMException('aborted', 'AbortError'));
                    });
                });
            };
        }""",
        _fake_rows(200),
    )
    page.evaluate("switchView('viewLibrary')")
    page.wait_for_function("document.querySelectorAll('#library .track').length === 200")
    page.evaluate(
        f"""async () => {{
            const box = {_scroller_of("#library")};
            for (let i = 0; i < 40; i++) {{
                box.scrollTop += 600;
                await new Promise((r) => setTimeout(r, 16));
            }}
        }}"""
    )
    page.wait_for_timeout(1500)
    covers = page.evaluate("window.__covers")
    on_screen = page.evaluate(
        """[...document.querySelectorAll('#library .track')].filter((row) => {
            const r = row.getBoundingClientRect();
            return r.bottom > 0 && r.top < innerHeight;
        }).map((row) => !!row.querySelector('img'))"""
    )
    assert on_screen and all(on_screen), on_screen  # what is on screen did arrive
    assert covers["peak"] <= 4, covers
    assert covers["asked"] <= 2 * len(on_screen) + 20, covers


def test_opened_list_asks_for_rows_on_screen_first(page):
    """The first four places went to the rows in the margin below the screen:
    the observer that marks rows on screen had not reported yet when the
    queue was served, and the rows the eye was on waited a whole round.
    Phone width: there the page itself scrolls, and the margin is real."""
    page.set_viewport_size({"width": 390, "height": 664})
    page.evaluate(
        """async (list) => {
            const asked = window.__asked = [];
            const real = window.fetch;
            const png = await (await real('/static/icon-180.png')).blob();
            window.fetch = (url, options = {}) => {
                const u = String(url);
                if (u.startsWith('/api/library?')) {
                    // Late enough that no scroll is fresh: the queue is served at once.
                    return new Promise((ok) => setTimeout(() => ok(new Response(list,
                        {headers: {'Content-Type': 'application/json'}})), 300));
                }
                if (!u.startsWith('/api/cover?')) return real(url, options);
                asked.push(decodeURIComponent(u).match(/Song \\d+/)[0]);
                return new Promise((ok) => setTimeout(() => ok(new Response(png)), 300));
            };
        }""",
        _fake_rows(200),
    )
    page.evaluate("switchView('viewLibrary')")
    page.wait_for_function("window.__asked.length >= 4")
    first = page.evaluate("window.__asked.slice(0, 4)")
    on_screen = page.evaluate(
        """[...document.querySelectorAll('#library .track')].filter((row) => {
            const r = row.getBoundingClientRect();
            return r.bottom > 0 && r.top < innerHeight;
        }).map((row) => row.textContent.match(/Song \\d+/)[0])"""
    )
    assert len(on_screen) >= 4 and set(first) <= set(on_screen), (first, on_screen)


def test_playlist_header_shows_the_playlist_opened_last(page):
    """The playlist header is one element. Opening B while A's first-track
    cover was on its way: A's answer finished, took B's job with it, and the
    header kept A's picture."""
    page.evaluate(
        """async () => {
            const real = window.fetch;
            const png = await (await real('/static/icon-180.png')).blob();
            window.fetch = (url, options = {}) => {
                const u = String(url);
                if (u.startsWith('/api/playlists/')) return Promise.resolve(new Response('', {status: 404}));
                if (!u.startsWith('/api/cover?')) return real(url, options);
                const wait = u.includes('A%2Fa.m4a') ? 400 : 50;
                return new Promise((ok) => setTimeout(() => ok(new Response(png)), wait));
            };
            window.playlistFirstTrack = (name) => Promise.resolve(name + '/' + name.toLowerCase() + '.m4a');
            const host = document.createElement('div');
            host.id = 'testHeader';
            host.style.cssText = 'position:fixed;top:0;left:0;width:100px;height:100px;z-index:99';
            document.body.appendChild(host);
            loadPlaylistCover(host, 'A');
        }"""
    )
    page.wait_for_timeout(200)  # A's first-track cover is on its way
    page.evaluate("loadPlaylistCover(document.getElementById('testHeader'), 'B')")
    page.wait_for_timeout(1000)
    shown = page.evaluate(
        """(() => {
            const img = document.querySelector('#testHeader img');
            if (!img) return 'letter ' + document.getElementById('testHeader').textContent;
            return img.src === coverUrls.get('track:' + THUMB_LARGE + ':B/b.m4a') ? 'B'
                : img.src === coverUrls.get('track:' + THUMB_LARGE + ':A/a.m4a') ? 'A' : 'other';
        })()"""
    )
    assert shown == "B", shown


def test_artist_photos_gone_from_screen_give_up_their_places(page):
    """An artist's photo is a trip to Deezer, up to a second. Rows scrolled
    away kept their places in the covers' queue until it came back, and the
    rows now on screen waited behind them."""
    page.evaluate("switchView('viewAdd')")  # no artists of the home page in the way
    page.wait_for_timeout(300)
    page.evaluate(
        """async () => {
            const started = window.__started = [];
            const real = window.fetch;
            window.fetch = (url, options = {}) => {
                const u = String(url);
                if (!u.startsWith('/api/artist-photo?')) return real(url, options);
                started.push(new URLSearchParams(u.split('?')[1]).get('name'));
                return new Promise((ok, fail) => {
                    const timer = setTimeout(() => ok(new Response('', {status: 404})), 1500);
                    if (options.signal) options.signal.addEventListener('abort', () => {
                        clearTimeout(timer);
                        fail(new DOMException('aborted', 'AbortError'));
                    });
                });
            };
            const box = document.createElement('div');
            box.id = 'testArtists';
            box.style.cssText = 'position:fixed;top:0;left:0;width:300px;z-index:99';
            document.body.appendChild(box);
            for (const name of ['Old 1', 'Old 2', 'Old 3', 'Old 4']) {
                const cover = labArtistCover(name, '', 96, false);
                cover.style.cssText = 'width:40px;height:40px';
                box.appendChild(cover);
            }
        }"""
    )
    page.wait_for_function("window.__started.length >= 2")
    page.evaluate(
        """() => {
            const box = document.getElementById('testArtists');
            box.replaceChildren();
            for (const name of ['New 1', 'New 2']) {
                const cover = labArtistCover(name, '', 96, false);
                cover.style.cssText = 'width:40px;height:40px';
                box.appendChild(cover);
            }
        }"""
    )
    page.wait_for_timeout(500)
    started = page.evaluate("window.__started")
    assert "New 1" in started and "New 2" in started, started


def test_audio_not_starved_by_covers(page, monkeypatch):
    """The browser keeps six connections to the service. Covers that take a
    second each (a cold thumbnail) held all six, and the track's link waited
    behind them; four at most leave room for the sound."""
    monkeypatch.setattr(library, "embedded_cover", lambda path: time.sleep(1.5))
    page.evaluate(
        """(list) => {
            const real = window.fetch;
            window.fetch = (url, options) => String(url).startsWith('/api/library?')
                ? Promise.resolve(new Response(list, {headers: {'Content-Type': 'application/json'}}))
                : real(url, options);
        }""",
        _fake_rows(30),
    )
    page.evaluate("switchView('viewLibrary')")
    page.wait_for_function("document.querySelectorAll('#library .track').length === 30")
    page.wait_for_timeout(300)  # the covers are on their way
    took = page.evaluate(
        """(async () => {
            const started = performance.now();
            const r = await fetch('/api/stream-url?path=' + encodeURIComponent('Loud Band/Singles/Loud.opus'),
                                  {headers: headers()});
            await r.json();
            return performance.now() - started;
        })()"""
    )
    assert took < 700, took


@pytest.mark.parametrize("missed_before", [False, True])
def test_finds_covers_share_the_cover_limit(page, missed_before):
    """Home asked for twelve finds' pictures and the next twelve at once, each
    a trip to Deezer through the service: all six connections were held for a
    second, and the library's covers (and the track's link) waited behind.
    Now they go through the covers' queue, two at a time at most — also those
    whose "no picture" from an earlier visit has expired."""
    finds = json.dumps(
        {
            "tracks": [
                {"title": f"Song {i}", "artist": f"Band {i}",
                 "cover": f"https://cdn-images.dzcdn.net/images/cover/{i:032x}/500x500.jpg"}
                for i in range(12)
            ]
        }
    )  # fmt: skip
    page.evaluate(
        """async ([finds, missedBefore]) => {
            const c = window.__finds = {inflight: 0, peak: 0, finds: 0, findsPeak: 0};
            if (missedBefore) {
                for (const f of JSON.parse(finds).tracks) coverUrls.set('find:' + f.cover, Date.now() - 1);
            }
            const real = window.fetch;
            const png = await (await real('/static/icon-180.png')).blob();
            window.fetch = (url, options = {}) => {
                const u = String(url);
                if (u.startsWith('/api/discover-external?')) {
                    return Promise.resolve(new Response(finds, {headers: {'Content-Type': 'application/json'}}));
                }
                const find = u.startsWith('/api/web-cover?');
                if (!find && !u.startsWith('/api/cover?')) return real(url, options);
                c.inflight += 1; c.peak = Math.max(c.peak, c.inflight);
                if (find) { c.finds += 1; c.findsPeak = Math.max(c.findsPeak, c.finds); }
                return new Promise((ok) => setTimeout(() => {
                    c.inflight -= 1;
                    if (find) c.finds -= 1;
                    ok(new Response(png));
                }, 150));
            };
            findsNext = null;
        }""",
        [finds, missed_before],
    )
    page.evaluate("switchView('viewHome')")
    page.wait_for_function("document.querySelectorAll('.is-find img').length === 12")
    page.wait_for_function("window.__finds.inflight === 0")
    finds_seen = page.evaluate("window.__finds")
    assert finds_seen["findsPeak"] <= 2 and finds_seen["peak"] <= 4, finds_seen


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


def test_a_play_heard_offline_keeps_the_time_it_was_heard(page, server):
    """Без сети прослушивание ждёт в очереди и уходит позже — со временем, когда
    трек звучал (начало прослушивания), а не когда вернулась сеть."""
    import json
    from datetime import datetime

    from adder import db

    open_library(page)
    page.evaluate("localStorage.setItem('pendingPlays', '[]')")
    page.evaluate(
        "playQueue([{path: 'Loud Band/Singles/Loud.opus', title: 'Loud', artist: 'Loud Band',"
        " duration: 12}], 0)"
    )
    page.wait_for_function("!player.audio.paused && player.audio.currentTime > 2")
    page.evaluate("player.audio.pause()")

    # No network: the play goes to the queue with the time it was heard.
    page.route("**/api/plays", lambda route: route.abort())
    heard = page.evaluate("Date.now() / 1000 - player.audio.currentTime")
    page.evaluate("reportPlay(false)")
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 1")
    queued = page.evaluate("JSON.parse(localStorage.getItem('pendingPlays'))[0]")
    assert abs(queued["heard_at"] - heard) < 2

    # The network is back some time later: the queued time travels unchanged.
    page.wait_for_timeout(1500)
    page.unroute("**/api/plays")
    sent = []
    page.on(
        "request",
        lambda r: sent.append(json.loads(r.post_data)) if r.url.endswith("/api/plays") else None,
    )
    page.evaluate("flushPendingPlays()")
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0")
    assert sent == [queued]
    row = db.db_query("SELECT played_at FROM plays ORDER BY id DESC LIMIT 1")[0]
    ended = datetime.fromtimestamp(int(queued["heard_at"] + queued["played_seconds"]))
    assert row["played_at"] == ended.strftime("%Y-%m-%d %H:%M:%S")


def test_plays_survive_rate_limit(page):
    """60 прослушиваний в минуту — предел сервера (429). Отказ по пределу терял
    живое прослушивание, а отправка накопленного вставала на первом отказе до
    следующего события «online», которого при живой сети не бывает."""
    import json

    open_library(page)
    page.evaluate("localStorage.setItem('pendingPlays', '[]')")
    answers = [429]  # then 200 for everything
    posts = []

    def answer(route):
        posts.append(json.loads(route.request.post_data))
        status = answers.pop(0) if answers else 200
        route.fulfill(status=status, json={"detail": "limit"} if status == 429 else {})

    page.route("**/api/plays", answer)

    # A live play answered 429 is kept for later, not dropped.
    page.evaluate(
        "player.queue = [{path: 'Loud Band/Singles/Loud.opus', title: 'Loud'}]; player.index = 0;"
        "player.started = true; player.reported = false; reportPlay(true)"
    )
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 1")

    # A backlog bigger than the limit: the third one is refused, and the rest
    # still go out on a timer -- the network never "comes back".
    backlog = [
        {"path": f"Loud Band/Singles/{n}.opus", "played_seconds": n, "source": "player"}
        for n in range(1, 6)
    ]
    page.evaluate("(items) => localStorage.setItem('pendingPlays', JSON.stringify(items))", backlog)
    answers[:] = [200, 200, 429]
    posts.clear()
    page.evaluate("PLAY_RETRY_MS = 300; flushPendingPlays()")
    page.wait_for_function(
        "JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0", timeout=5000
    )
    assert posts == backlog[:3] + backlog[2:]


def test_play_queue_does_not_hammer_the_server(page):
    """Очередь прослушиваний не стучится раз в минуту вечно: отказ в доступе
    (токен сменили) ждёт входа, а не таймера; без сети ждёт «online»; каждая
    новая неудача — пауза вдвое дольше."""
    import json

    open_library(page)
    item = {"path": "Loud Band/Singles/Loud.opus", "played_seconds": 5, "source": "player"}
    page.evaluate("(i) => localStorage.setItem('pendingPlays', JSON.stringify([i]))", item)
    answers: list[int] = []
    posts: list[float] = []

    def answer(route):
        posts.append(time.monotonic())
        status = answers.pop(0) if answers else 200
        route.fulfill(status=status, json={})

    page.route("**/api/plays", answer)

    # 401: no timer at all; logging in again sends the backlog.
    answers[:] = [401]
    page.evaluate("PLAY_RETRY_MS = 100; playRetryFailures = 0; flushPendingPlays()")
    page.wait_for_function("!flushingPlays")
    page.wait_for_timeout(600)
    assert len(posts) == 1
    assert page.evaluate("playRetryTimer") is None
    page.evaluate("(t) => { document.getElementById('tokenInput').value = t; saveToken(); }", TOKEN)
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0")
    assert len(posts) == 2

    # Server errors: each retry waits about twice as long as the one before.
    page.evaluate("(i) => localStorage.setItem('pendingPlays', JSON.stringify([i]))", item)
    answers[:] = [503, 503, 503]
    posts.clear()
    page.evaluate("PLAY_RETRY_MS = 150; playRetryFailures = 0; flushPendingPlays()")
    page.wait_for_function(
        "JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0", timeout=5000
    )
    gaps = [b - a for a, b in zip(posts, posts[1:], strict=False)]
    assert len(gaps) == 3, gaps
    # Floors, not ratios: a slow round trip under load only lengthens a gap.
    assert all(gap >= 0.15 * 2**i * 0.9 for i, gap in enumerate(gaps)), gaps
    assert page.evaluate("playRetryFailures") == 0  # a success starts over

    # Offline: the "online" event sends it, no timer ticks meanwhile.
    page.evaluate("(i) => localStorage.setItem('pendingPlays', JSON.stringify([i]))", item)
    posts.clear()
    page.context.set_offline(True)
    page.evaluate("flushPendingPlays()")
    page.wait_for_function("!flushingPlays")
    assert page.evaluate("playRetryTimer") is None
    page.context.set_offline(False)
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0")
    assert len(posts) == 1
    assert json.loads(page.evaluate("localStorage.getItem('pendingPlays')")) == []


def test_play_queue_follows_the_server_not_the_network_flag(page):
    """A server that answered is reachable whatever navigator.onLine says (the
    laptop without Wi-Fi plays from localhost); and once a live play gets
    through, the backlog goes too instead of sitting out a long backoff."""
    open_library(page)
    item = {"path": "Loud Band/Singles/Loud.opus", "played_seconds": 5, "source": "player"}
    answers: list[int] = []
    posts: list[dict] = []

    def answer(route):
        posts.append(route.request.post_data)
        route.fulfill(status=answers.pop(0) if answers else 200, json={})

    page.route("**/api/plays", answer)

    # 503 with navigator.onLine false: the server answered, so a timer is set.
    page.evaluate("(i) => localStorage.setItem('pendingPlays', JSON.stringify([i]))", item)
    page.evaluate(
        "Object.defineProperty(navigator, 'onLine', {get: () => false, configurable: true})"
    )
    answers[:] = [503]
    page.evaluate("PLAY_RETRY_MS = 60000; playRetryFailures = 4; flushPendingPlays()")
    page.wait_for_function("!flushingPlays")
    assert page.evaluate("playRetryTimer !== null")
    page.evaluate("delete navigator.onLine")

    # The backoff is long now; a live play that gets through sends the backlog.
    posts.clear()
    page.evaluate(
        "player.queue = [{path: 'Quiet/Singles/Quiet.m4a', title: 'Quiet'}]; player.index = 0;"
        "player.started = true; player.reported = false; reportPlay(true)"
    )
    page.wait_for_function("JSON.parse(localStorage.getItem('pendingPlays') || '[]').length === 0")
    assert len(posts) == 2
    assert page.evaluate("playRetryFailures") == 0


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


@pytest.mark.parametrize("size", [(1266, 603), (390, 844)], ids=["laptop-150%", "phone"])
def test_the_download_bar_sits_on_the_player_and_hides_the_rows(
    page, fake_deezer_artist, monkeypatch, size
):
    """02.10.2026: на ноутбуке кнопка висела посреди списка (высота плеера
    учитывалась дважды), а неактивная была полупрозрачной — сквозь неё
    читались строки."""
    many = [
        {"artist": "Quiet", "title": f"Песня {n}", "duration": 100 + n, "have": False}
        for n in range(40)
    ]
    disco = {
        "artist": {"id": "77", "name": "Quiet"},
        "hidden": 0,
        "releases": [
            {"id": "1", "title": "Много", "kind": "album", "year": "", "cover": "", "tracks": many}
        ],
    }
    monkeypatch.setattr(discography, "discography", lambda aid, rows: disco)
    page.set_viewport_size({"width": size[0], "height": size[1]})
    open_library(page)
    row(page, "Loud").locator(".track-info").click()  # полоса плеера на месте
    page.wait_for_function("!document.getElementById('player').hidden")
    page.evaluate("switchView('viewAdd')")
    page.locator("#artistQuery").fill("quiet")
    page.locator("#artistImport button.primary").click()
    page.locator(".disco-choice").first.click()
    page.wait_for_selector(".disco-track")
    page.evaluate("document.querySelectorAll('.disco-track')[15].scrollIntoView({block: 'center'})")
    page.wait_for_timeout(200)
    bar, floor, opacity = page.evaluate(
        "(() => { const b = document.querySelector('.disco-actions').getBoundingClientRect();"
        " const tops = [...document.querySelectorAll('#player, .tabbar')]"
        "   .map(n => n.getBoundingClientRect()).filter(r => r.height && r.top > innerHeight / 2)"
        "   .map(r => r.top);"
        " return [b.bottom, Math.min(innerHeight, ...tops),"
        "   getComputedStyle(document.querySelector('.disco-actions button')).opacity]; })()"
    )
    assert abs(bar - floor) <= 1, f"полоса «Скачать» не у нижнего края: {bar} против {floor}"
    assert opacity == "1"


# --- Каждая кнопка на каждом экране (план, шаг 1.4) --------------------------
#
# Обход: на каждом экране «Обложки» — на ноутбуке и на телефоне — нажать по
# очереди каждую видимую доступную кнопку, после каждой вернуть экран как был и
# убедиться, что страница не бросила исключение, не написала ошибку в консоль,
# не получила ответ-ошибку и не упёрлась в CSP (последнее и исключения ловит ещё
# и фикстура page).

_MEASURE_UI = Path(__file__).resolve().parent.parent / "scripts" / "measure_ui.py"
_spec = importlib.util.spec_from_file_location("measure_ui_for_sweep", _MEASURE_UI)
assert _spec and _spec.loader
measure_ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure_ui)

SWEEP_CONTROLS = "button, [role=button], [role=menuitem], [role=tab], [role=switch], a[href]"

# Чего обход НЕ нажимает — и почему. Это и есть список непокрытого: всё
# остальное видимое и доступное на перечисленных экранах нажимается.
# По доступному имени (aria-label, иначе текст, иначе title; без регистра):
SWEEP_SKIP_NAMES = {
    "удал": "удаляет: трек, подборку, скачанное (Удалить…, Удалить подборку…, Удалить всё скачанное)",
    "вернуть фото": "удаляет свою шапку артиста",
    "скачать": "качает: Скачать недостающее…, Скачать N треков, Скачать всё",
    "на телефон": "качает всю подборку на устройство",
    "youtube": "ищет на YouTube (yt-dlp) — «Найти … на YouTube и скачать»",
    "забыть ключ": "выход: стирает ключ и уводит на экран входа",
    "обложка…|шапка…": "открывает выбор файла",
    "сохранить": "переписывает теги файла в общей для модуля фонотеке",
    "найти обложки|измерить громкость": "фоновая работа по всей фонотеке: переписывает файлы, ищет обложки в сети",
}
# По селектору:
SWEEP_SKIP_SELECTORS = {
    '[onclick^="addTracks"]': "«Добавить» в разделе «Добавить» — загрузка ссылок с YouTube",
    '[onclick^="runSearch"]': "«Искать» — поиск на YouTube (yt-dlp)",
    '[onclick^="runAlbumSearch"]': "«Альбом» — поиск альбома в Deezer",
    "label.file-button, label.drop-zone, input[type=file]": "выбор файлов",
    ".handle, [role=separator]": "ручки перетаскивания (край рельсы и т. п.)",
    'a[target="_blank"]': "ссылки, уводящие со страницы",
}
# Строки и карточки одного списка (треки, полка главной, подборки в рельсе,
# очередь) рисует один код: нажимаются кнопки первых SWEEP_ROWS строк каждого
# списка, остальные считаются и пропускаются — иначе обход рос бы вместе с
# фонотекой. SWEEP_CAP — предохранитель на экран; сейчас на самом длинном 13.
SWEEP_ROW = ".track, .o-card, .o-artist-row, .o-mood, .rail-item, .queue-row"
SWEEP_ROWS = 2
SWEEP_CAP = 30
SWEEP_SETTLE_MS = 40

_SWEEP_ENUMERATE = """([scope, controls, names, selectors, rowSelector, maxRows]) => {
    const skipName = new RegExp(names, "i");
    for (const n of document.querySelectorAll("[data-sweep]")) n.removeAttribute("data-sweep");
    const found = [], skipped = [], seen = new Set(), lists = new Map();
    let repeated = 0;
    for (const root of document.querySelectorAll(scope)) {
        const inside = [...(root.matches(controls) ? [root] : []), ...root.querySelectorAll(controls)];
        for (const n of inside) {
            if (seen.has(n)) continue;
            seen.add(n);
            if (!n.checkVisibility({ checkOpacity: true, visibilityProperty: true })) continue;
            const box = n.getBoundingClientRect();
            if (!box.width || !box.height) continue;
            if (n.disabled || n.getAttribute("aria-disabled") === "true" || n.closest("[inert]")) continue;
            const row = n.closest(rowSelector);
            if (row) {
                const rows = lists.get(row.parentElement) || new Set();
                lists.set(row.parentElement, rows);
                if (!rows.has(row) && rows.size >= maxRows) { repeated += 1; continue; }
                rows.add(row);
            }
            const label = n.labels && n.labels.length ? n.labels[0].textContent : "";
            const name = (n.getAttribute("aria-label") || n.textContent || label || n.title || "")
                .replace(/\\s+/g, " ").trim().slice(0, 60);
            if (skipName.test(name) || selectors.some((s) => n.matches(s) || n.querySelector(s))) {
                skipped.push(name);
                continue;
            }
            n.dataset.sweep = String(found.length);
            found.push(name);
        }
    }
    return { found, skipped, repeated, state: (__STATE__)() };
}"""

# Где мы (раздел, режим фонотеки, большой плеер и что в нём) и что поверх
# (меню, таймер сна, диалог). Поверх закрывает Escape, «где» — открытие экрана.
_SWEEP_STATE = """() => ({
    place: [activeView, libraryMode, sheetOpen(), sheetPanel],
    over: [!!openMenu, !document.getElementById("playerSleepMenu").hidden,
           document.querySelectorAll("[role=dialog]:not(#player)").length],
})"""

DIALOG = "[role=dialog]:not(#player)"


def _sweep_enumerate(page, scope):
    """Пометить кнопки экрана data-sweep=N; вернуть их имена, пропущенные,
    число повторных строк и состояние страницы (_SWEEP_STATE)."""
    return page.evaluate(
        _SWEEP_ENUMERATE.replace("__STATE__", _SWEEP_STATE),
        [
            scope,
            SWEEP_CONTROLS,
            "|".join(f"(?:{k})" for k in SWEEP_SKIP_NAMES),
            list(SWEEP_SKIP_SELECTORS),
            SWEEP_ROW,
            SWEEP_ROWS,
        ],
    )


def _sweep_calm(page, sheet=None):
    """Закрыть меню, формы и диалоги; большой плеер оставить, только если он
    открыт с панелью sheet ("" — без панели), иначе свернуть.

    Открытая форма (теги под строкой, поиск текста песни) держит экран от
    перерисовки — её закрывает её же «Отмена», как человек."""
    collapse = page.evaluate(
        """(sheet) => {
            closeTrackMenu();
            closeSleepMenu();
            for (const b of document.querySelectorAll("button"))
                if (b.textContent.trim() === "Отмена" && b.checkVisibility()) b.click();
            for (const d of document.querySelectorAll("[role=dialog]:not(#player)")) d.remove();
            return sheetOpen() && (sheet == null || (sheetPanel || "") !== sheet);
        }""",
        sheet,
    )
    if collapse:
        page.evaluate("collapsePlayer()")
        page.wait_for_function("!sheetOpen() && !window.sheetBackPending")


def _sweep_track(page):
    """Плеер с треком, который этот Chromium умеет играть (Opus)."""
    if page.evaluate("!document.getElementById('player').hidden"):
        return
    page.evaluate(
        "labIndex().then(rows => playQueue(rows.filter(t => t.path.endsWith('.opus')), 0, 'manual'))"
    )
    page.wait_for_function("!document.getElementById('player').hidden")


def _sweep_view(view):
    def open_view(page):
        _sweep_calm(page)
        page.evaluate(f"switchView('{view}')")
        page.wait_for_function(f"activeView === '{view}'")

    return open_view


def _sweep_library(mode):
    def open_library_mode(page):
        _sweep_calm(page)
        page.evaluate(f"openLibrary('{mode}')")
        page.wait_for_function(
            f"activeView === 'viewLibrary' && libraryMode === '{mode}'"
            " && document.querySelector('#library > *')"
        )

    return open_library_mode


def _sweep_artist(page):
    _sweep_calm(page)
    page.evaluate("openArtistPage('Quiet')")
    page.wait_for_function(
        "activeView === 'viewArtist' && document.querySelector('#artistBody h1')"
    )


def _sweep_album(page):
    _sweep_calm(page)
    page.evaluate(
        "labIndex().then(rows => openAlbumPage(groupIntoAlbums(rows).find(g => g.tracks.length)))"
    )
    page.wait_for_function(
        "activeView === 'viewAlbum' && document.querySelector('#albumBody .track')"
    )


def _sweep_mood(page):
    # Настроения строятся по анализу звука, а его здесь нет: подборку
    # подкладываем в данные главной, страницу рисует настоящий код.
    _sweep_calm(page)
    page.evaluate(
        "homeDataForMoods = {moods: [{key: 'sweep', name: 'Обход', hint: 'для проверки',"
        " tracks: [{path: 'Loud Band/Singles/Loud.opus', title: 'Loud', artist: 'Loud Band'}]}]};"
        " openMoodPage('sweep')"
    )
    page.wait_for_function(
        "activeView === 'viewMood' && document.querySelector('#moodBody .track')"
    )


def _sweep_playlist(page):
    _sweep_calm(page)
    page.evaluate("openPlaylist('Обход')")
    page.wait_for_function(
        "activeView === 'viewPlaylist' && document.querySelector('#playlistTracks .playlist-track')"
    )


def _sweep_search(page):
    _sweep_calm(page)
    page.evaluate("switchView('viewSearch')")
    page.locator("#searchEverywhere").fill("loud")
    page.wait_for_selector("#searchBody .track-title")


def _sweep_bar(page):
    _sweep_calm(page)
    _sweep_track(page)


def _sweep_sheet(panel):
    """Большой плеер с панелью: "" — как его раскрывает обложка, "lyrics" —
    кнопка текста, "queue" — кнопка очереди."""

    def open_sheet(page):
        _sweep_track(page)
        wide = page.evaluate("WIDE.matches")
        want = panel or ("lyrics" if wide else "")  # на компьютере справа всегда текст
        _sweep_calm(page, sheet=want)
        if page.evaluate("sheetOpen()"):
            return
        run = {"": "expandPlayer('art')", "lyrics": "playerLyrics()", "queue": "playerQueue()"}
        page.evaluate(run[panel])
        page.wait_for_function("p => sheetOpen() && (sheetPanel || '') === p", want)
        if panel == "queue":
            page.locator("#playQueue .queue-row").first.wait_for()

    return open_sheet


def _sweep_menu_shown(page, menu):
    """Меню видно и доиграло появление (menu-in: от прозрачного к видимому):
    иначе его пункты ещё прозрачны и обход их не видит."""
    shown = page.locator(f"{menu}:visible").first
    shown.wait_for()
    shown.evaluate("m => Promise.all(m.getAnimations().map(a => a.finished))")


def _sweep_menu(open_screen, button, menu="[role=menu]"):
    """Экран, а поверх него открытое меню: нажимаются пункты меню."""

    def open_menu(page):
        open_screen(page)
        target = button(page) if callable(button) else page.locator(button).first
        target.click()
        _sweep_menu_shown(page, menu)

    return open_menu


def _sweep_row_menu(open_screen, rows):
    return _sweep_menu(
        open_screen,
        lambda page: page.locator(rows).first.get_by_role("button", name="Что сделать с треком"),
    )


def _sweep_player_menu(button, menu="[role=menu]"):
    """Меню плеера: из полосы, а где кнопки в полосе нет (телефон) — из
    большого плеера."""

    def open_menu(page):
        _sweep_track(page)
        _sweep_calm(page, sheet="" if page.evaluate("!WIDE.matches") else None)
        if not page.locator(button).is_visible():
            _sweep_sheet("")(page)
        # «Перемешать» при включённом перемешивании выключает его, а не
        # открывает меню; предыдущие нажатия могли его включить.
        page.evaluate("if (player.shuffle) setShuffle(false)")
        page.locator(button).click()
        _sweep_menu_shown(page, menu)

    return open_menu


def _sweep_new_playlist(page):
    if page.locator("#railNewPlaylist").is_visible():
        _sweep_view("viewHome")(page)
        page.locator("#railNewPlaylist").click()
    else:  # на телефоне «+» в заголовке «Подборок»
        _sweep_view("viewPlaylists")(page)
        page.locator("#topAdd").click()
    _sweep_menu_shown(page, DIALOG)


def _sweep_tag_editor(page):
    _sweep_row_menu(_sweep_library("tracks"), "#library .track")(page)
    page.get_by_role("menuitem", name="Изменить теги…").click()
    page.locator("form.edit-tags").wait_for()


# (экран, как открыть, где на нём кнопки)
SWEEP_SCREENS = [
    ("рельса, вкладки и шапка", _sweep_view("viewHome"), ".side, .tabbar, .topbar"),
    ("главная", _sweep_view("viewHome"), "#viewHome"),
    ("фонотека: треки", _sweep_library("tracks"), "#viewLibrary"),
    ("фонотека: артисты", _sweep_library("artists"), "#viewLibrary"),
    ("фонотека: альбомы", _sweep_library("albums"), "#viewLibrary"),
    ("артист", _sweep_artist, "#viewArtist"),
    ("альбом", _sweep_album, "#viewAlbum"),
    ("настроение", _sweep_mood, "#viewMood"),
    ("все подборки", _sweep_view("viewPlaylists"), "#viewPlaylists"),
    ("подборка", _sweep_playlist, "#viewPlaylist"),
    ("поиск", _sweep_search, "#viewSearch"),
    ("добавить", _sweep_view("viewAdd"), "#viewAdd"),
    ("служба", _sweep_view("viewService"), "#viewService"),
    ("полоса плеера", _sweep_bar, "#player"),
    ("большой плеер", _sweep_sheet(""), "#player"),
    ("текст песни", _sweep_sheet("lyrics"), "#viewLyrics"),
    ("очередь", _sweep_sheet("queue"), "#playQueue"),
    ("изменить теги", _sweep_tag_editor, "form.edit-tags"),
    ("⋯ строки фонотеки", _sweep_row_menu(_sweep_library("tracks"), "#library .track"), "[role=menu]"),
    ("⋯ строки подборки", _sweep_row_menu(_sweep_playlist, "#playlistTracks .track"), "[role=menu]"),
    ("⋯ строки артиста", _sweep_row_menu(_sweep_artist, "#artistBody .track"), "[role=menu]"),
    ("⋯ подборки", _sweep_menu(_sweep_playlist, "#playlistMore"), "[role=menu]"),
    ("⋯ артиста", _sweep_menu(_sweep_artist, "#artistBody .o-more-round"), "[role=menu]"),
    ("перемешать фонотеку", _sweep_menu(_sweep_library("tracks"), "#libraryShuffle"), "[role=menu]"),
    ("перемешать подборку", _sweep_menu(_sweep_playlist, "[onclick^='openPlaylistShuffle']"), "[role=menu]"),
    ("⋯ плеера", _sweep_player_menu("#playerMore"), "[role=menu]"),
    ("перемешать в плеере", _sweep_player_menu("#playerShuffle"), "[role=menu]"),
    ("таймер сна", _sweep_player_menu("#playerSleep", "#playerSleepMenu"), "#playerSleepMenu"),
    ("новая подборка", _sweep_new_playlist, DIALOG),
]  # fmt: skip


def _sweep_open(page, open_screen, where):
    try:
        open_screen(page)
    except playwright.Error as exc:
        pytest.fail(f"{where}: экран не открылся: {str(exc).splitlines()[0]}")


@pytest.mark.parametrize("size", [(1280, 800), (390, 844)], ids=["laptop", "phone"])
def test_every_control_on_every_screen_has_no_console_errors(page, server, size):
    """Plan step 1.4, AC 1-2: every visible, enabled control on every screen,
    one at a time, with the screen restored after each, leaves no page error,
    no console error, no failed response and no CSP violation behind.

    What is not clicked is listed in SWEEP_SKIP_NAMES / SWEEP_SKIP_SELECTORS;
    SWEEP_CAP bounds a screen. "Failed to load resource" in the console is not
    an error by itself: responses are judged by measure_ui.bad_response (a 404
    for a cover is how the service says "no picture")."""
    (server["root"] / "Обход.m3u").write_text(
        "Loud Band/Singles/Loud.opus\nQuiet/Singles/Quiet.m4a\n", encoding="utf-8"
    )
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.evaluate("playlists()")
    page.wait_for_function("knownPlaylists.some(p => p.name === 'Обход')")

    where = ["загрузка страницы"]
    problems: list[str] = []

    def on_console(message):
        if message.type == "error" and not message.text.startswith("Failed to load resource"):
            problems.append(f"{where[0]}: console: {message.text}")

    def on_response(response):
        if not response.url.startswith(server["url"]):
            return
        path = response.url.split(server["url"], 1)[-1].split("?")[0]
        if measure_ui.bad_response(response.status, path, response.request.method):
            problems.append(f"{where[0]}: {response.status} {path}")

    page.on("console", on_console)
    page.on("response", on_response)
    page.on("pageerror", lambda exc: problems.append(f"{where[0]}: pageerror: {exc}"))

    clicked: dict[str, list[str]] = {}
    skipped: dict[str, list[str]] = {}
    repeated: dict[str, int] = {}
    for screen, open_screen, scope in SWEEP_SCREENS:
        where[0] = f"{screen}: открытие"
        _sweep_open(page, open_screen, where[0])
        now = _sweep_enumerate(page, scope)
        base, total = now["state"], len(now["found"])
        skipped[screen] = now["skipped"]
        repeated[screen] = now["repeated"]
        clicked[screen] = []
        for index in range(min(total, SWEEP_CAP)):
            if index >= len(now["found"]):
                break
            name = now["found"][index]
            where[0] = f"{screen}: «{name}»"
            try:
                page.locator(f'[data-sweep="{index}"]').click(timeout=2000)
            except playwright.Error:
                # Список мог перерисоваться между перечнем и нажатием (очередь
                # после смены трека) — метка пропала вместе со старым узлом.
                # Перечислить заново и нажать ещё раз; не вышло — это ошибка.
                now = _sweep_enumerate(page, scope)
                if index >= len(now["found"]):
                    break
                name = now["found"][index]
                where[0] = f"{screen}: «{name}»"
                try:
                    page.locator(f'[data-sweep="{index}"]').click(timeout=2000)
                except playwright.Error as exc:
                    problems.append(f"{where[0]}: не нажимается: {str(exc).splitlines()[0]}")
            clicked[screen].append(name)
            page.wait_for_timeout(SWEEP_SETTLE_MS)
            assert page.url.startswith(server["url"]), f"{where[0]}: ушли со страницы"

            # Вернуть экран: открытое поверх закрыть Escape, как человек;
            # ушли с экрана или он стал другим — открыть его заново.
            where[0] = f"{screen}: возврат после «{name}»"
            over = page.evaluate(_SWEEP_STATE)["over"]
            if over != base["over"] and any(over):
                page.keyboard.press("Escape")
            now = _sweep_enumerate(page, scope)
            if now["state"] != base or len(now["found"]) != total:
                _sweep_open(page, open_screen, where[0])
                now = _sweep_enumerate(page, scope)
    where[0] = "после обхода"
    page.wait_for_timeout(300)

    report = "\n".join(
        f"{s}: {len(c)} {c} / не нажаты {skipped[s]} / повторных строк {repeated[s]}"
        for s, c in clicked.items()
    )
    print(f"\n[{size[0]}x{size[1]}]\n{report}")
    assert problems == [], "\n".join(problems)
    # Обход, который ничего не нашёл, ничего и не проверил.
    empty = [s for s, c in clicked.items() if not c and not skipped[s]]
    assert empty == [], f"на этих экранах не нашлось ни одной кнопки: {empty}"
