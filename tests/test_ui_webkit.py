"""The page on Safari's engine, the way the iPhone opens it (Playwright + WebKit).

test_ui.py drives Chromium; the phone runs WebKit, which differs exactly where
this player lives: audio, the service worker, storage. Same service, same
temporary library (the server fixture is shared with test_ui.py), an iPhone
viewport with touch.

Runs only where Playwright's WebKit build starts: in CI on Ubuntu. On Arch its
libraries are missing (libvpx.so.9 is in neither the repositories nor the AUR),
so locally conftest leaves this file out and says so in the run's header.
"""

from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

# The shared server points every path into its own temp dir.
OWN_RUNTIME = True

from test_ui import TOKEN, XSS_TITLE, open_library, row, row_action, server  # noqa: E402, F401


@pytest.fixture(scope="module")
def iphone():
    with playwright.sync_playwright() as p:
        instance = p.webkit.launch()
        yield instance, p.devices["iPhone 13"]
        instance.close()


@pytest.fixture()
def page(server, iphone):  # noqa: F811
    browser, device = iphone
    context = browser.new_context(**device)
    context.add_init_script(f"localStorage.setItem('token', '{TOKEN}');")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(server["url"] + "/")
    page.wait_for_function("typeof switchView === 'function'")
    yield page
    context.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"


def test_the_phone_layout_opens_with_its_four_tabs(page):
    tabs = page.locator(".tabbar .tab").all_inner_texts()
    assert [t.strip() for t in tabs] == ["Главная", "Фонотека", "Подборки", "Поиск"]


def test_the_library_renders_titles_as_text(page):
    open_library(page)
    assert row(page, XSS_TITLE).is_visible()
    assert page.evaluate("window.pwned") is None


def test_downloads_are_offered(page):
    # Service worker and Cache API: what keeps the library on the phone.
    assert page.evaluate("offline.supported") is True
    assert page.evaluate("navigator.serviceWorker.ready.then(r => Boolean(r.active))") is True


def test_an_aac_track_plays(page):
    # The phone's own library is AAC/Opus in .m4a; WebKit plays it through GStreamer.
    open_library(page)
    row_action(page, "Quiet", "Играть")
    page.wait_for_function("!player.audio.paused && player.audio.currentTime > 0.2", timeout=15000)
