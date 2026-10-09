"""Переход «как диджей» в настоящем браузере (Playwright + Chromium).

Служба — как в tests/test_ui.py, но со своей фонотекой: три трека по 70 с
розового шума (у шума корреляция однозначна, у синуса — нет: плеер находит
место по звуку). Связку сводит не librosa, а простая подмена с теми же краями:
чистый конец A, наложение, чистое начало B. Проверяется плеер: что он встаёт
на A и на B до отсчёта, передаёт трек, а кнопки посреди перехода не ломают
очередь. Как звучит само сведение — tests/test_transition_render.py и слух.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
np = pytest.importorskip("numpy")

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
    lyrics,
    navidrome,
    outside,
    playlists,
    runtime,
    similar,
    sync,
    transitions,
)

TOKEN = "ui-mix-token"
SR = 48000
TRACKS = {
    "a": "Alpha/Singles/One.opus",
    "b": "Bravo/Singles/Two.opus",
    "c": "Charlie/Singles/Three.opus",
}
LEAD, MIX, TAIL = 4.0, 4.0, 8.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _noise_track(root: Path, rel: str, seed: int):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=d=70:c=pink:r=48000:a=0.3:seed={seed}",
         "-ac", "2", "-c:a", "libopus", "-b:a", "128k", str(path)],
        check=True,
    )  # fmt: skip
    title = Path(rel).stem
    ingest.write_tags(
        path, enrich.TrackInfo(album=title, artists=[rel.split("/")[0]]), title, None, None
    )


def _decode(path: Path, start: float, length: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ss", f"{start:.6f}", "-t", f"{length:.6f}",
         "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)


def fake_render(job: dict) -> dict:
    """Связка с теми же краями, что у настоящей: 4 с A, наложение 4 с, 8 с B."""
    a, b = Path(job["a"]), Path(job["b"])
    duration = float(
        json.loads(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(a)],
                capture_output=True, text=True, check=True,
            ).stdout
        )["format"]["duration"]
    )  # fmt: skip
    a_from = duration - 14.0
    tail_a = _decode(a, a_from, LEAD + MIX)
    head_b = _decode(b, 0.0, MIX + TAIL)
    n = int(MIX * SR)
    t = np.linspace(0, 1, n, dtype=np.float32)[:, None]
    out_a = tail_a[int(LEAD * SR) : int(LEAD * SR) + n]
    mixed = out_a * np.cos(t * np.pi / 2) + head_b[:n] * np.sin(t * np.pi / 2)
    bridge = np.concatenate([tail_a[: int(LEAD * SR)], mixed, head_b[n : n + int(TAIL * SR)]])
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
         "-c:a", "flac", "-sample_fmt", "s16", job["out"]],
        input=bridge.astype(np.float32).tobytes(), check=True,
    )  # fmt: skip
    return {
        "version": transitions.RENDER_VERSION,
        "case": "test",
        "sample_rate": SR,
        "a_from": a_from,
        "lead": LEAD,
        "b_from": MIX,
        "b_solo_at": LEAD + MIX,
        "length": len(bridge) / SR,
    }


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    from adder import app as app_module

    tmp = tmp_path_factory.mktemp("ui-mix")
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
        "TRANSITIONS_DIR": tmp / "transitions",
    }.items():
        mp.setattr(runtime, name, value)
    mp.setattr(playlists, "HISTORY_DIR", tmp / "history")
    mp.setattr(covers, "COVERS_DIR", tmp / "covers")
    mp.setattr(lyrics, "CACHE_DIR", tmp / "lyrics")
    mp.setattr(navidrome, "configured", lambda: False)
    mp.setattr(sync, "start", lambda: None)
    mp.setattr(lyrics, "start_backfill", lambda: None)
    mp.setattr(lyrics, "_ask", lambda *a, **k: {})
    mp.setattr(lyrics, "_search", lambda *a, **k: {})
    mp.setattr(listenbrainz, "start", lambda: None)
    mp.setattr(outside, "candidates", lambda *a, **k: [])
    mp.setattr(analysis, "analyse_track", lambda path: None)
    mp.setattr(similar, "_ask", lambda *a, **k: None)
    mp.setattr(similar, "_ask_top", lambda *a, **k: None)
    mp.setattr(similar, "_ask_picture", lambda *a, **k: None)
    mp.setattr(discography, "start", lambda queue_one: None)
    mp.setattr(enrich, "lookup", lambda a, t: None)
    mp.setattr(enrich, "musicbrainz_lookup", lambda a, t: None)
    mp.setattr(ingest, "check_dependencies", lambda: {"ffmpeg": "ok", "js_runtime": "ok"})
    mp.setattr(transitions, "_render", fake_render)
    mp.setattr(transitions, "_available", lambda: True)
    transitions.reset()

    for seed, rel in enumerate(TRACKS.values(), start=1):
        _noise_track(root, rel, seed)
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
    transitions.reset()
    mp.undo()
    runtime.shutdown_event.clear()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        yield instance
        instance.close()


CSP_WATCH = """
window.__csp = [];
document.addEventListener("securitypolicyviolation", (e) =>
    window.__csp.push(e.effectiveDirective + " " + e.blockedURI));
"""


def _open(server, browser, init: str = ""):
    context = browser.new_context()
    context.add_init_script(f"localStorage.setItem('token', '{TOKEN}');")
    context.add_init_script(CSP_WATCH)
    if init:
        context.add_init_script(init)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    plays: list[dict] = []
    events: list[dict] = []

    def on_request(request):
        if request.method == "POST" and request.url.endswith("/api/plays"):
            plays.append(json.loads(request.post_data or "{}"))
        if request.method == "POST" and request.url.endswith("/api/player-event"):
            events.append(json.loads(request.post_data or "{}"))

    page.on("request", on_request)
    page.goto(server["url"] + "/")
    wait(page, "typeof switchView === 'function' && typeof mixTick === 'function'")
    return context, page, errors, plays, events


def wait(page, expression, timeout=30.0):
    """Ожидание через evaluate: строгий CSP страницы не даёт Playwright eval."""
    deadline = time.monotonic() + timeout
    while True:
        value = page.evaluate(expression)
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"не дождались: {expression}")
        page.wait_for_timeout(20)


@pytest.fixture()
def opened(server, browser):
    context, page, errors, plays, events = _open(server, browser)
    yield page, plays, events
    violations = page.evaluate("window.__csp || []")
    context.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"
    assert violations == [], f"blocked by the page's CSP: {violations}"


START_DJ = """async () => {
    setFade('dj');
    const r = await fetch('/api/library', { headers: headers() });
    const rows = await r.json();
    const order = ORDER;
    const tracks = order.map(p => rows.find(t => t.path === p));
    playQueue(tracks, 0);
}""".replace("ORDER", json.dumps(list(TRACKS.values())))


def start_and_reach_the_bridge(page):
    page.evaluate(START_DJ)
    wait(page, "!player.audio.paused && player.audio.currentTime > 0.2")
    wait(page, "mix.prep && mix.prep.status === 'ready'")
    plan = page.evaluate("mix.prep.plan")
    # Перемотка к началу связки (секунда до неё) — ждать минуту незачем.
    page.evaluate(f"player.audio.currentTime = {plan['a_from'] - 1.0}")
    return plan


def test_the_next_track_is_handed_over_sample_exact(opened):
    page, plays, events = opened
    plan = start_and_reach_the_bridge(page)
    first = page.evaluate("player.audio")
    assert first is not None

    # Связка заменила A: в этот момент они звучат одно и то же — отсчёт в отсчёт.
    wait(page, "mix.run && mix.run.phase === 'bridge'", timeout=15)
    lag = page.evaluate(
        """async () => {
            const run = mix.run;
            const tapA = mix.nodes.get(run.a).tap;
            const tapS = new AudioWorkletNode(mix.ctx, 'mix-tap');
            run.src.connect(tapS);
            tapS.connect(mix.silent);
            const [ra, rs] = await Promise.all([record(tapA, 0.4), record(tapS, 0.4)]);
            // Совместить по кадрам часов и найти лучший сдвиг в ±10 мс.
            const shift = rs.frame - ra.frame;
            let best = -Infinity, at = 0;
            for (let lag = -480; lag <= 480; lag++) {
                let dot = 0;
                for (let i = 600; i < ra.data.length - 600; i++) {
                    const j = i - shift + lag;
                    if (j >= 0 && j < rs.data.length) dot += ra.data[i] * rs.data[j];
                }
                if (dot > best) { best = dot; at = lag; }
            }
            return at;
        }"""
    )
    assert lag == 0

    # Трек B стал текущим, стык — меньше половины миллисекунды.
    wait(page, "player.index === 1", timeout=20)
    last = page.evaluate("mix.last")
    assert last["docked"] is True and abs(last["delta"]) <= 0.0005, last
    b_at = page.evaluate("player.audio.currentTime")
    assert plan["b_from"] < b_at < plan["b_from"] + TAIL + 2
    assert page.evaluate("!player.audio.paused && player.queue[player.index].path") == TRACKS["b"]
    # Прежний элемент отпущен, A записан доигравшим, лишнего «дальше» нет.
    assert page.evaluate("deck.filter(el => el !== player.audio)[0].getAttribute('src')") is None
    page.wait_for_timeout(1500)
    assert page.evaluate("player.index") == 1
    a_play = [p for p in plays if p.get("path") == TRACKS["a"]]
    assert len(a_play) == 1 and a_play[0]["skipped"] is False
    assert any(e["event"] == "mix" for e in events)
    assert not any(e["event"] == "mix-fail" for e in events), events
    # Без выхода из тишины: начало B уже прозвучало в связке.
    assert page.evaluate("player.fadeLevel") == 1


WEBKIT_TIMING = """
    // Как WebKitGTK: currentTime опережает звук в графе на секунду, перемотка
    // ложится мимо на несколько миллисекунд.
    const real = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime');
    Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {
        get() { const t = real.get.call(this); return t > 0 && !this.paused ? t + 1.0 : t; },
        set(v) { real.set.call(this, Math.max(0, v + (Math.random() * 0.016 - 0.008))); },
        configurable: true,
    });
"""


def test_webkit_timing_is_made_up_for(server, browser):
    """Слушать A по кругу, пока не узнан; B — подогнать связкой, а не перемоткой."""
    context, page, errors, _, events = _open(server, browser, WEBKIT_TIMING)
    try:
        page.evaluate(START_DJ)
        wait(page, "!player.audio.paused && player.audio.currentTime > 1.2")
        wait(page, "mix.prep && mix.prep.status === 'ready'")
        plan = page.evaluate("mix.prep.plan")
        page.evaluate(f"player.audio.currentTime = {plan['a_from'] - 1.0}")
        wait(page, "player.index === 1 && mix.last", timeout=40)
        last = page.evaluate("mix.last")
        assert last["docked"] is True and abs(last["delta"]) <= 0.0005, last
        assert last["lag"] == pytest.approx(1.0, abs=0.06)
        # Подгонка связкой: последний шаг до стыка — мелкий сдвиг, не перемотка.
        assert any(0.5 < abs(ms) <= 12 for ms, _ in last["trace"]), last
        assert any(e["event"] == "mix" for e in events)
        assert errors == []
    finally:
        context.close()


def test_pause_during_the_bridge_goes_back_to_the_outgoing_track(opened):
    page, _, _ = opened
    start_and_reach_the_bridge(page)
    wait(page, "mix.run && mix.run.phase === 'bridge'", timeout=15)
    page.evaluate("togglePlay()")
    assert page.evaluate("mix.run === null && player.index === 0 && player.audio.paused")
    # A снова со своей громкостью — не остался приглушённым.
    level = page.evaluate("mix.nodes.get(player.audio).gain.gain.value")
    assert level > 0.5
    # И дальше обычный конец: связку для этой пары плеер больше не пробует.
    assert page.evaluate("mix.prep.status") == "done"


def test_next_during_the_bridge_starts_the_next_track_from_the_top(opened):
    page, _, _ = opened
    start_and_reach_the_bridge(page)
    wait(page, "mix.run && mix.run.phase === 'bridge'", timeout=15)
    page.evaluate("nextTrack()")
    wait(page, "player.index === 1 && !player.audio.paused")
    assert page.evaluate("mix.run") is None
    assert page.evaluate("player.audio.currentTime") < 3


def test_the_chosen_transition_is_marked_and_remembered(opened):
    page, _, _ = opened
    page.evaluate("setFade('dj')")
    assert (
        page.evaluate(
            "document.querySelector('#playerFadeBox [data-fade=dj]').getAttribute('aria-checked')"
        )
        == "true"
    )
    assert page.evaluate("localStorage.getItem('playerFade')") == "dj"
    assert page.evaluate("player.fadeSeconds") == 3  # где не выйдет — затухание
    page.evaluate("setFade(6)")
    assert page.evaluate("player.djMode") is False


def test_where_the_page_cannot_set_volume_no_audio_graph_is_made(server, browser):
    """iPhone: громкость только кнопками, Web Audio обрывает фон — графа нет вовсе."""
    ios = """
        Object.defineProperty(HTMLMediaElement.prototype, 'volume',
            { get() { return 1; }, set(v) {}, configurable: true });
        window.__contexts = 0;
        const Real = window.AudioContext;
        window.AudioContext = function (...args) { window.__contexts += 1; return new Real(...args); };
        localStorage.setItem('playerFade', 'dj');
    """
    context, page, errors, _, _ = _open(server, browser, ios)
    try:
        assert page.evaluate("player.volumeAdjustable") is False
        page.evaluate(START_DJ)
        wait(page, "!player.audio.paused && player.audio.currentTime > 0.5")
        page.wait_for_timeout(1500)
        assert page.evaluate("window.__contexts") == 0
        assert page.evaluate("mix.ctx") is None
        assert errors == []
    finally:
        context.close()
