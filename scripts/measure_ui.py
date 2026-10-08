#!/usr/bin/env python3
"""Как быстро открывается и листается плеер — в браузере, по живому сервису.

Меряет то, что обещают цели скорости (план полного разбора, AC 14–17):

* **готовность с нуля** — новый контекст, пустой кэш, без service worker:
  от начала перехода до метки ``app-ready`` (её ставит app.js, когда первый
  экран нарисован с данными), цель ≤ 1.5 с;
* **готовность повторно** — второй переход в том же контексте, цель ≤ 0.5 с;
* **обложки** — от последнего события прокрутки до момента, когда на экране
  не осталось ни одной недогруженной обложки, цель ≤ 0.3 с;
* **прокрутка** — вся фонотека (с «Показать ещё» до конца), задачи главного
  потока длиннее 50 мс, цель — ни одной;
* **поиск** — от ввода последней буквы до нарисованного результата, задержка
  ввода входит, цель ≤ 0.15 с; ``--idle`` добавляет случай «минуту не трогали,
  потом ищут».

Профили: ``laptop`` (Chromium 1280×800) и ``phone`` (Chromium под iPhone 13,
процессор медленнее в 4 раза, 60 мс задержки сети). Каждая цифра — медиана
нескольких прогонов (``--runs``, по умолчанию 10) и p90.

    .venv/bin/python scripts/measure_ui.py                    # всё, оба профиля
    .venv/bin/python scripts/measure_ui.py --budget           # выход ≠ 0, если цель не выполнена
    .venv/bin/python scripts/measure_ui.py --smoke            # страница открылась без ошибок
    .venv/bin/python scripts/measure_ui.py --only ready,search --profile laptop

Только читает: страница ничего не добавляет, не играет и не пишет в журнал
прослушиваний. Токен берётся из adder/.env и никуда не выводится.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://127.0.0.1:8787"

# Цели — миллисекунды; long_tasks — сколько задач длиннее 50 мс допустимо.
TARGETS = {
    "ready_cold": 1500.0,
    "ready_warm": 500.0,
    "covers": 300.0,
    "long_tasks": 0.0,
    "search": 150.0,
    "search_library": 150.0,
    "search_idle": 150.0,
}
METRICS = ("ready", "covers", "scroll", "search")
PROFILES = ("laptop", "phone")


# ---------------------------------------------------------------- чистые части


def stats(values: Sequence[float | None]) -> dict:
    """Медиана, p90, число удачных прогонов и неудачных (None — замер не дождался)."""
    got = [v for v in values if v is not None]
    failed = len(values) - len(got)
    if not got:
        return {"median": None, "p90": None, "n": 0, "failed": failed}
    ordered = sorted(got)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {
        "median": round(statistics.median(ordered), 1),
        "p90": round(p90, 1),
        "n": len(ordered),
        "failed": failed,
    }


def misses(results: dict) -> list[str]:
    """Какие цели не выполнены: «профиль/метрика: медиана > цели».

    Метрика без единого замера — тоже промах: «не измерено» не значит «быстро».
    """
    found = []
    for profile, metrics in results.items():
        for name, value in metrics.items():
            target = TARGETS.get(name)
            if target is None:
                continue
            median = value.get("median")
            if median is None:
                found.append(f"{profile}/{name}: нет замера")
            elif value.get("failed"):
                found.append(
                    f"{profile}/{name}: {value['failed']} прогон(ов) не дождались результата"
                )
            elif median > target:
                found.append(f"{profile}/{name}: {median:g} > {target:g}")
    return found


def read_token(env_file: Path) -> str:
    """API_TOKEN из adder/.env — так же, как его читает python-dotenv."""
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line.startswith("API_TOKEN="):
            continue
        value = line.split("=", 1)[1].strip()
        if value[:1] in ("'", '"') and value.endswith(value[:1]) and len(value) > 1:
            return value[1:-1]
        return value.split(" #", 1)[0].strip()
    raise SystemExit(f"API_TOKEN не найден в {env_file}")


def _hits(rows: list[dict], q: str) -> int:
    fields = ("title", "artist", "album")
    return sum(any(q in (r.get(f) or "").lower() for f in fields) for r in rows)


def search_queries(rows: list[dict], count: int, seed: int = 7) -> list[str]:
    """Подстроки названий длиной 1–6 букв: так ищут на самом деле.

    Только такие, где последняя буква меняет выдачу: иначе страница ничего не
    перерисует, и «результат нарисован» не наступит вовсе.
    """
    rnd = random.Random(seed)
    words = [
        w for r in rows for w in (r.get("title") or "").split() if any(ch.isalpha() for ch in w)
    ]
    out: list[str] = []
    for _ in range(count * 50):
        if not words or len(out) >= count:
            break
        word = rnd.choice(words)
        q = word[: rnd.randint(1, min(6, len(word)))].lower()
        if _hits(rows, q) != _hits(rows, q[:-1]):
            out.append(q)
    return out


def table(results: dict) -> str:
    lines = [f"{'профиль':<8} {'метрика':<15} {'медиана':>9} {'p90':>9} {'n':>3} {'цель':>7}"]
    for profile, metrics in results.items():
        for name, value in metrics.items():
            median = "—" if value["median"] is None else f"{value['median']:g}"
            p90 = "—" if value["p90"] is None else f"{value['p90']:g}"
            target = TARGETS.get(name)
            lines.append(
                f"{profile:<8} {name:<15} {median:>9} {p90:>9} {value['n']:>3} "
                f"{'' if target is None else f'{target:g}':>7}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------- браузер

# Счётчик запросов обложек в пути: «догрузилось» — это и картинки готовы, и
# ни один запрос не висит (у трека без обложки картинки не будет никогда).
COVER_PROBE = """
(() => {
  window.__coverInflight = 0;
  const real = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const cover = url.includes("/api/cover");
    if (cover) window.__coverInflight++;
    const p = real.apply(this, arguments);
    // Отменённый (строка ушла с экрана) — тоже конец; отказ здесь не нужен.
    if (cover) p.finally(() => { window.__coverInflight--; }).catch(() => {});
    return p;
  };
  // Обложка «в пути» — от вызова fetchTrackCover до вставки картинки или отказа:
  // помечается сам квадрат, чтобы «догрузилось» судилось по тем, что на экране.
  addEventListener("DOMContentLoaded", () => {
    const orig = window.fetchTrackCover;
    if (typeof orig !== "function") return;
    // Ответ обёрнутой отдаётся дальше: по нему очередь обложек (app.js)
    // считает, сколько запросов в пути и сделан ли этот.
    window.fetchTrackCover = function (host, path, size) {
      host.__coverPending = true;
      const result = orig.apply(this, arguments);
      Promise.resolve(result).catch(() => {})
        .finally(() => setTimeout(() => { host.__coverPending = false; }, 0));
      return result;
    };
  });
  window.__longTasks = [];
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__longTasks.push([e.startTime, e.duration]);
    }).observe({ type: "longtask", buffered: true });
  } catch (e) { /* не Chromium */ }
})();
"""

SCROLLER_JS = """
(sel) => {
  let el = document.querySelector(sel);
  while (el && el !== document.body) {
    const s = getComputedStyle(el);
    if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight) return el;
    el = el.parentElement;
  }
  return document.scrollingElement;
}
"""

COVERS_SETTLED_JS = """
() => {
  const vh = innerHeight, vw = innerWidth;
  for (const host of document.querySelectorAll(".cover, .o-cover")) {
    const r = host.getBoundingClientRect();
    if (r.width === 0 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
    if (host._coverJob || host.__coverPending) return false;   // ещё не начата или в пути
    const img = host.querySelector("img");
    if (img && (!img.complete || img.naturalWidth === 0)) return false;
  }
  return true;
}
"""

# Время от ввода до первой перерисовки результата — внутри страницы, по её часам.
# Взводится перед вводом последней буквы. Начало — время события клавиши
# (keydown, а если буква вставлена без него — input): задержка ввода входит.
# Конец — кадр после того, как текст выдачи изменился; появление картинок и
# фоновый опрос без изменений текст не меняют и концом не считаются.
SEARCH_ARM_JS = """
([inputSel, bodySels]) => {
  const input = document.querySelector(inputSel);
  const bodies = bodySels.map(s => document.querySelector(s)).filter(Boolean);
  const sig = () => bodies.map(b => b.innerText).join("\\u0001");
  const before = sig();
  window.__searchDone = new Promise((resolve) => {
    let t0 = null;
    const mark = (e) => { if (t0 === null) t0 = e.timeStamp; };
    input.addEventListener("keydown", mark, { capture: true, once: true });
    input.addEventListener("input", mark, { capture: true, once: true });
    const obs = new MutationObserver(() => {
      if (t0 === null || sig() === before) return;
      obs.disconnect();
      requestAnimationFrame(() => resolve(performance.now() - t0));
    });
    for (const b of bodies) obs.observe(b, { childList: true, subtree: true, characterData: true });
    setTimeout(() => { obs.disconnect(); resolve(null); }, 5000);
  });
}
"""


def wait_until(page, expression: str, timeout_ms: int = 20000):
    """Ждать, пока выражение на странице не станет истинным.

    Не через wait_for_function: Playwright проверяет строку через eval внутри
    страницы, а её Content-Security-Policy eval запрещает. page.evaluate идёт
    через протокол отладки, и CSP его не касается.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        value = page.evaluate(expression)
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"не дождался: {expression}")
        page.wait_for_timeout(10)


PHONE_RTT_MS = 60


class DelayProxy:
    """TCP-прокси на 127.0.0.1: каждый кусок данных в обе стороны ждёт
    one_way_ms, так что запрос с ответом получает 2 × one_way_ms сверху.

    Задержка CDP (Network.emulateNetworkConditions) действует только на
    запросы самой страницы. Запросы, которые перехватывает service worker
    (обложки, фонотека, главная), он делает сам — и они шли вовсе без
    задержки: 7 мс вместо 66 (замер 08.10.2026). Через прокси идёт всё, что
    уходит к службе, как через настоящую сеть.
    """

    def __init__(self, host: str, port: int, one_way_ms: float):
        self.host, self.port, self.delay = host, port, one_way_ms / 1000
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(started,), daemon=True)
        self.thread.start()
        if not started.wait(10):
            raise RuntimeError("прокси с задержкой не запустился")
        self.url = f"http://127.0.0.1:{self.local_port}"

    def _run(self, started: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(
            asyncio.start_server(self._client, "127.0.0.1", 0)
        )
        self.local_port = self.server.sockets[0].getsockname()[1]
        started.set()
        self.loop.run_forever()

    async def _client(self, reader, writer) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(self.host, self.port)
        except OSError:
            writer.close()
            return
        try:
            await asyncio.gather(
                self._pipe(reader, up_writer), self._pipe(up_reader, writer), return_exceptions=True
            )
        finally:
            for w in (writer, up_writer):
                w.close()

    async def _pipe(self, reader, writer) -> None:
        """Куски уходят в том же порядке, каждый — через delay после прихода."""
        queue: asyncio.Queue = asyncio.Queue()

        async def send() -> None:
            while True:
                due, data = await queue.get()
                wait = due - self.loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if not data:
                    if writer.can_write_eof():
                        writer.write_eof()
                    return
                writer.write(data)
                await writer.drain()

        sender = asyncio.ensure_future(send())
        try:
            while data := await reader.read(65536):
                queue.put_nowait((self.loop.time() + self.delay, data))
        except OSError:
            pass
        queue.put_nowait((self.loop.time() + self.delay, b""))
        await sender

    def close(self) -> None:
        async def stop() -> None:
            self.server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)  # their finally closes sockets
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


class Session:
    def __init__(self, pw, url: str, token: str, profile: str):
        self.pw = pw
        self.url = url.rstrip("/")
        self.token = token
        self.profile = profile
        # Телефону — задержка сети на всём пути к службе (см. DelayProxy). По
        # https прокси не встать (сертификат не на 127.0.0.1) — там остаётся
        # задержка CDP, только для запросов страницы.
        self.proxy = None
        parts = urlsplit(self.url)
        if profile == "phone" and parts.scheme == "http":
            self.proxy = DelayProxy(
                parts.hostname or "127.0.0.1", parts.port or 80, PHONE_RTT_MS / 2
            )
            self.url = self.proxy.url
        try:
            self.browser = pw.chromium.launch()
        except Exception:
            if self.proxy is not None:
                self.proxy.close()
            raise

    def close(self) -> None:
        self.browser.close()
        if self.proxy is not None:
            self.proxy.close()

    def context(self, service_workers: str = "allow"):
        options: dict = {"service_workers": service_workers}
        if self.profile == "phone":
            options.update(self.pw.devices["iPhone 13"])
            options.pop("default_browser_type", None)
        else:
            options["viewport"] = {"width": 1280, "height": 800}
        ctx = self.browser.new_context(**options)
        ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(self.token)});")
        ctx.add_init_script(COVER_PROBE)
        return ctx

    def page(self, ctx):
        page = ctx.new_page()
        if self.profile == "phone":
            cdp = ctx.new_cdp_session(page)
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
            if self.proxy is None:
                cdp.send("Network.enable")
                cdp.send(
                    "Network.emulateNetworkConditions",
                    {
                        "offline": False,
                        "latency": PHONE_RTT_MS,
                        "downloadThroughput": -1,
                        "uploadThroughput": -1,
                    },
                )
        return page

    def ready_at(self, page) -> float:
        wait_until(page, "performance.getEntriesByName('app-ready').length > 0")
        return float(page.evaluate("performance.getEntriesByName('app-ready')[0].startTime"))

    def open(self, ctx, path: str = "/"):
        page = self.page(ctx)
        page.goto(self.url + path)
        self.ready_at(page)
        return page


def measure_ready(s: Session, runs: int) -> dict:
    cold = []
    for _ in range(runs):
        ctx = s.context(service_workers="block")
        page = s.page(ctx)
        page.goto(s.url + "/")
        cold.append(s.ready_at(page))
        ctx.close()
    warm = []
    ctx = s.context()
    page = s.open(ctx)
    for _ in range(runs):
        page.goto(s.url + "/")
        warm.append(s.ready_at(page))
    ctx.close()
    return {"ready_cold": stats(cold), "ready_warm": stats(warm)}


def _open_library(page) -> None:
    page.evaluate("switchView('viewLibrary')")
    page.wait_for_selector("#library .track")


def _scroll_and_settle(page, scroller, delta: int) -> float | None:
    """Прокрутить, дождаться 150 мс тишины, вернуть мс до догрузки обложек."""
    last = page.evaluate(
        """async ([el, delta]) => {
            let last = performance.now();
            const on = () => { last = performance.now(); };
            (el === document.scrollingElement ? window : el).addEventListener("scroll", on);
            for (let i = 0; i < 6; i++) {
                el.scrollTop += delta / 6;
                await new Promise(r => setTimeout(r, 16));
            }
            while (performance.now() - last < 150) await new Promise(r => setTimeout(r, 10));
            (el === document.scrollingElement ? window : el).removeEventListener("scroll", on);
            return last;
        }""",
        [scroller, delta],
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if page.evaluate(COVERS_SETTLED_JS):
            return float(page.evaluate("performance.now()")) - last
        page.wait_for_timeout(10)
    return None


def measure_covers(s: Session, runs: int) -> dict:
    """10 позиций в списке треков и в сетке альбомов."""
    values = []
    ctx = s.context()
    page = s.open(ctx)
    for mode in ("tracks", "albums"):
        _open_library(page)
        page.evaluate(f"setLibraryMode({json.dumps(mode)})")
        page.wait_for_timeout(500)
        scroller = page.evaluate_handle(SCROLLER_JS, "#library")
        for _ in range(runs):
            got = _scroll_and_settle(page, scroller, 900)
            if got is not None:
                values.append(got)
    ctx.close()
    return {"covers": stats(values)}


def measure_scroll(s: Session) -> dict:
    """Вся фонотека: «Показать ещё» до конца, потом шагами по 600 px раз в 16 мс."""
    ctx = s.context()
    page = s.open(ctx)
    _open_library(page)
    page.evaluate("setLibraryMode('tracks')")
    while True:
        more = page.locator("#library .library-more")
        if not more.count():
            break
        more.click()
        wait_until(page, "!document.querySelector('#library .library-more[disabled]')")
    scroller = page.evaluate_handle(SCROLLER_JS, "#library")
    gaps = page.evaluate(
        """async (el) => {
            window.__longTasks = [];
            const start = performance.now();
            const gaps = [];
            let prev = start, running = true;
            const frame = (t) => {
                gaps.push(t - prev);
                prev = t;
                if (running) requestAnimationFrame(frame);
            };
            requestAnimationFrame(frame);
            el.scrollTop = 0;
            while (el.scrollTop + el.clientHeight < el.scrollHeight - 2) {
                el.scrollTop += 600;
                await new Promise(r => setTimeout(r, 16));
            }
            await new Promise(r => setTimeout(r, 300));
            running = false;
            const long = window.__longTasks.filter(([t, d]) => t >= start && d > 50);
            return {
                long: long.length,
                longest: Math.max(0, ...long.map(([, d]) => d)),
                frameGaps: gaps.filter(g => g > 50).length,
                rows: document.querySelectorAll("#library .track").length,
            };
        }""",
        scroller,
    )
    ctx.close()
    result = stats([float(gaps["long"])])
    result.update(
        {
            "longest_ms": round(gaps["longest"], 1),
            "frame_gaps_over_50ms": gaps["frameGaps"],
            "rows": gaps["rows"],
        }
    )
    return {"long_tasks": result}


def search_once(page, input_sel: str, bodies: list[str], q: str, wait_ms: int = 700):
    """Набрать всё, кроме последней буквы, подождать, ввести её настоящей клавишей."""
    page.fill(input_sel, q[:-1])
    page.wait_for_timeout(wait_ms)
    page.evaluate(SEARCH_ARM_JS, [input_sel, bodies])
    page.focus(input_sel)
    page.keyboard.type(q[-1])
    return page.evaluate("window.__searchDone")


def measure_search(s: Session, runs: int, idle: bool) -> dict:
    ctx = s.context()
    page = s.open(ctx)
    rows = page.evaluate(
        "labIndex().then(rows => rows.map(r =>"
        " ({title: r.title, artist: r.artist, album: r.album})))"
    )
    queries = search_queries(rows, max(runs, 20))
    out = {}

    page.evaluate("switchView('viewSearch')")
    page.wait_for_selector("#searchEverywhere")
    everywhere = [search_once(page, "#searchEverywhere", ["#searchBody"], q) for q in queries]
    out["search"] = stats(everywhere)

    _open_library(page)
    lib = ["#library", "#libraryEmpty"]
    out["search_library"] = stats([search_once(page, "#librarySearch", lib, q) for q in queries])

    if idle:
        page.evaluate("switchView('viewSearch')")
        once = search_once(page, "#searchEverywhere", ["#searchBody"], queries[0], wait_ms=61_000)
        out["search_idle"] = stats([once])
    ctx.close()
    return out


def bad_response(status: int, path: str, method: str = "GET") -> bool:
    """Ответ, который в смоуке считается ошибкой.

    404 на обложку — не ошибка: так сервис говорит «картинки нет» (у трека её
    нет в файле, у подборки не загружена своя), и страница рисует букву.
    Браузер всё равно пишет об этом в консоль — поэтому ответы судятся здесь,
    по адресу, а сообщения консоли «Failed to load resource» не считаются.
    """
    if status < 400:
        return False
    # «Картинки нет» по замыслу: обложка трека или подборки, фото артиста.
    picture = (
        path.startswith("/api/cover") or path.endswith("/cover") or path == "/api/artist-photo"
    )
    return not (status == 404 and picture and method == "GET")


def smoke(pw, url: str, token: str) -> list[str]:
    """Страница открылась, дошла до «готово», без ошибок страницы и ответов."""
    s = Session(pw, url, token, "laptop")
    problems: list[str] = []

    def on_console(m) -> None:
        if m.type == "error" and not m.text.startswith("Failed to load resource"):
            problems.append(f"console: {m.text}")

    def on_response(r) -> None:
        path = urlsplit(r.url).path
        if bad_response(r.status, path, r.request.method):
            problems.append(f"{r.status} {path}")

    try:
        ctx = s.context()
        page = s.page(ctx)
        page.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
        page.on("console", on_console)
        page.on("response", on_response)
        page.goto(s.url + "/")
        try:
            s.ready_at(page)
        except Exception as exc:  # noqa: BLE001 — любой провал ожидания и есть ответ
            problems.append(f"app-ready не наступил: {exc}")
        page.wait_for_timeout(1000)
        ctx.close()
    finally:
        s.close()
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--env", type=Path, default=PROJECT / "adder" / ".env")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--profile", default=",".join(PROFILES))
    parser.add_argument("--only", default=",".join(METRICS))
    parser.add_argument(
        "--idle", action="store_true", help="ещё «минуту не трогали, потом ищут» (+61 с)"
    )
    parser.add_argument(
        "--budget", action="store_true", help="выход 1, если хоть одна цель не выполнена"
    )
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--out", type=Path, help="JSON-отчёт сюда")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    token = read_token(args.env)
    with sync_playwright() as pw:
        if args.smoke:
            problems = smoke(pw, args.url, token)
            for p in problems:
                print(p)
            print("smoke: ok" if not problems else f"smoke: {len(problems)} проблем")
            return 1 if problems else 0

        only = set(args.only.split(","))
        results: dict[str, dict] = {}
        for profile in args.profile.split(","):
            s = Session(pw, args.url, token, profile)
            try:
                got: dict = {}
                if "ready" in only:
                    got.update(measure_ready(s, args.runs))
                if "covers" in only:
                    got.update(measure_covers(s, args.runs))
                if "scroll" in only:
                    got.update(measure_scroll(s))
                if "search" in only:
                    got.update(measure_search(s, args.runs, args.idle))
                results[profile] = got
            finally:
                s.close()

    print(table(results))
    report = {
        "url": args.url,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "targets": TARGETS,
        "results": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    missed = misses(results)
    if missed:
        print("\nцели не выполнены:\n  " + "\n  ".join(missed))
    return 1 if (args.budget and missed) else 0


if __name__ == "__main__":
    sys.exit(main())
