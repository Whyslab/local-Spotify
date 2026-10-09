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
    # У этого A файл кончается посреди связки (на её 6-й секунде): так бывает
    # и у настоящих треков — дальше звучит только связка.
    "d": "Delta/Singles/Four.opus",
}
LEAD, MIX, TAIL = 4.0, 4.0, 8.0
EARLY_END = 6.0  # с от начала связки, когда кончается файл трека "d"


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
    a_from = duration - (EARLY_END if a.name == "Four.opus" else 14.0)
    tail_a = _decode(a, a_from, LEAD + MIX)
    tail_a = np.pad(tail_a, ((0, max(0, int((LEAD + MIX) * SR) - len(tail_a))), (0, 0)))
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


def wait_event(page, events, name, timeout=5.0):
    """События запросов Playwright отдаёт Python не сразу — дождаться нужного."""
    deadline = time.monotonic() + timeout
    while not any(e["event"] == name for e in events):
        if time.monotonic() > deadline:
            raise AssertionError(f"нет события {name}: {events}")
        page.wait_for_timeout(50)


@pytest.fixture()
def opened(server, browser):
    context, page, errors, plays, events = _open(server, browser)
    yield page, plays, events
    violations = page.evaluate("window.__csp || []")
    context.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"
    assert violations == [], f"blocked by the page's CSP: {violations}"


def start_dj(*keys: str, at: int = 0) -> str:
    order = [TRACKS[k] for k in keys or ("a", "b", "c")]
    return """async () => {
        setFade('dj');
        const r = await fetch('/api/library', { headers: headers() });
        const rows = await r.json();
        const tracks = ORDER.map(p => rows.find(t => t.path === p));
        playQueue(tracks, AT);
    }""".replace("ORDER", json.dumps(order)).replace("AT", str(at))


START_DJ = start_dj()


def start_and_reach_the_bridge(page, *keys: str, at: int = 0):
    page.evaluate(start_dj(*keys, at=at))
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
    wait_event(page, events, "mix")
    assert not any(e["event"] == "mix-fail" for e in events), events
    # Без выхода из тишины: начало B уже прозвучало в связке.
    assert page.evaluate("player.fadeLevel") == 1


WEBKIT_TIMING = """
    // Как WebKitGTK: currentTime опережает звук в графе на секунду, перемотка
    // ложится мимо на несколько миллисекунд.
    const real = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime');
    Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {
        get() { const t = real.get.call(this); return t > 0 && !this.paused ? t + 1.0 : t; },
        // 3–8 мс в любую сторону — не ближе: иначе B мог бы встать без подгонки.
        set(v) {
            const off = (0.003 + Math.random() * 0.005) * (Math.random() < 0.5 ? -1 : 1);
            real.set.call(this, Math.max(0, v + off));
        },
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
        wait_event(page, events, "mix")
        assert errors == []
    finally:
        context.close()


def test_pause_during_the_bridge_goes_back_to_the_outgoing_track(opened):
    page, _, _ = opened
    start_and_reach_the_bridge(page)
    wait(page, "mix.run && mix.run.phase === 'bridge'", timeout=15)
    page.evaluate("togglePlay()")
    assert page.evaluate("mix.run === null && player.index === 0 && player.audio.paused")
    # И кнопка, и система знают, что это пауза.
    assert (
        page.evaluate("document.getElementById('playerToggle').getAttribute('aria-label')")
        == "Играть"
    )
    assert page.evaluate("navigator.mediaSession.playbackState") == "paused"
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


def reach_the_end_of_a(page):
    """Трек "d": его файл кончается посреди связки — дальше звучит только она."""
    start_and_reach_the_bridge(page, "d", "b", "c")
    wait(page, "mix.run && mix.run.phase === 'bridge' && player.audio.ended", timeout=20)
    assert page.evaluate("audioPaused()") is False  # музыка-то играет
    # И кнопка с системой это знают: «пауза» приходит раньше «конца», рисуют по ней.
    label = "document.getElementById('playerToggle').getAttribute('aria-label')"
    assert page.evaluate(label) == "Пауза"
    assert page.evaluate("navigator.mediaSession.playbackState") == "playing"


def test_the_sleep_timer_stops_the_music_after_the_outgoing_track_ended(opened):
    page, _, _ = opened
    reach_the_end_of_a(page)
    page.evaluate("player.sleep = { until: Date.now() - 1 }; sleepTick()")
    page.wait_for_timeout(800)
    assert page.evaluate("mix.run") is None
    assert page.evaluate("audioPaused()") is True
    assert page.evaluate("deck.every(el => el.paused)") is True


def test_until_the_end_of_the_track_set_during_the_bridge_stops_after_it(opened):
    page, _, _ = opened
    start_and_reach_the_bridge(page, "d", "b", "c")
    wait(page, "mix.run && mix.run.phase === 'bridge'", timeout=15)
    page.evaluate("setSleep('track')")
    wait(page, "player.audio.ended || mix.run === null", timeout=20)
    page.wait_for_timeout(800)
    assert page.evaluate("mix.run") is None
    assert page.evaluate("deck.every(el => el.paused)") is True
    assert page.evaluate("player.index") == 0


def test_a_seek_after_the_outgoing_track_ended_plays_it_from_there(opened):
    page, _, _ = opened
    reach_the_end_of_a(page)
    page.evaluate("player.audio.currentTime = 30")
    page.wait_for_timeout(1000)
    assert page.evaluate("mix.run") is None
    assert page.evaluate("player.index") == 0
    assert page.evaluate("!player.audio.paused") is True
    assert 30 < page.evaluate("player.audio.currentTime") < 33


HANG_B = """
    // Поток трека B «завис»: play() не кончается ничем.
    const realPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
        if (window.__hangB && typeof player !== 'undefined' && this !== player.audio) {
            return new Promise(() => {});
        }
        return realPlay.call(this);
    };
"""


def test_a_stuck_incoming_track_does_not_leave_silence(server, browser):
    context, page, errors, _, events = _open(server, browser, HANG_B)
    try:
        start_and_reach_the_bridge(page, "d", "b", "c")
        page.evaluate("window.__hangB = true")
        wait(page, "mix.run && mix.run.phase === 'bridge'", timeout=15)
        # Связка (16 с) кончилась — плеер сам уходит на B обычным путём.
        wait(page, "mix.run === null && player.index === 1 && !player.audio.paused", timeout=30)
        wait_event(page, events, "mix-fail")
        assert any(e["event"] == "mix-fail" and "завис" in e.get("detail", "") for e in events), (
            events
        )
        assert errors == []
    finally:
        context.close()


def test_the_queue_shifting_under_the_transition_does_not_cancel_it(opened):
    page, _, _ = opened
    # Очередь C, A, B, играет A; перед самым переходом C уходит из очереди
    # (так её сдвигает, например, возврат из умного перемешивания).
    start_and_reach_the_bridge(page, "c", "a", "b", at=1)
    page.evaluate(
        """() => {
            player.queue.splice(0, 1);
            player.index -= 1;
            player.order = player.order.filter(i => i !== 0).map(i => i - 1);
            player.orderAt = player.order.indexOf(player.index);
        }"""
    )
    wait(page, "player.index === 1 && mix.last", timeout=30)
    assert page.evaluate("mix.last.docked") is True
    assert page.evaluate("player.queue[player.index].path") == TRACKS["b"]


def test_mute_does_not_deafen_the_transition(opened):
    page, _, _ = opened
    page.evaluate("setFade('dj')")
    page.evaluate("toggleMute()")
    start_and_reach_the_bridge(page)
    wait(page, "player.index === 1 && mix.last", timeout=30)
    assert page.evaluate("mix.last.docked") is True
    gain = "mix.nodes.get(player.audio).gain.gain.value"
    assert page.evaluate(gain) == 0  # без звука — и B без звука
    page.evaluate("toggleMute()")
    page.wait_for_timeout(100)
    assert page.evaluate(gain) > 0.5


def test_a_quiet_passage_is_found_next_to_a_louder_repeat(opened):
    """Сходство считается по форме, а не по громкости: тихое место не
    подменяется громким повтором той же музыки."""
    page, _, _ = opened
    at = page.evaluate(
        """() => {
            let seed = 7;
            const noise = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };
            const n = 48000;
            const quiet = new Float32Array(n).map(() => noise() * 0.02);
            const ref = new Float32Array(2 * n);
            ref.set(quiet);
            for (let i = 0; i < n; i++) ref[n + i] = quiet[i] * 10 + noise() * 0.2;
            return locate(quiet.subarray(10000, 34000), ref);
        }"""
    )
    assert at["at"] == 10000 and at["score"] > 0.99, at
