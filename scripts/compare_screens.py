#!/usr/bin/env python3
"""Два варианта плеера рядом: одинаково ли выглядят все экраны «Обложки».

Для правок, которые обязаны ничего не менять на вид (удаление мёртвого CSS,
план полного разбора, шаг 2.4). Из двух ревизий (по умолчанию ``HEAD~`` и
``HEAD``) ``git archive`` выкладывает по копии во временную папку; каждая
поднимает свою службу на одинаковой заглушечной фонотеке (три коротких трека,
подборка «Обход», все внешние сервисы заглушены, как в ``tests/test_ui.py``).
Chromium открывает в обеих одни и те же экраны — их список и способы открыть
взяты из обхода ``tests/test_ui.py`` (SWEEP_SCREENS) — на ширине ноутбука и
телефона и сравнивает снимки попиксельно.

Чтобы отличить настоящую разницу от шума (часы, случайность, анимации), в
каждой точке снимается ещё и **контроль** — вторая страница той же базы.
Экран, где контроль не совпал с базой, называется нестабильным: о нём ничего
сказать нельзя, и скрипт выходит с ошибкой так же, как при разнице.

    .venv/bin/python scripts/compare_screens.py                 # HEAD~ против HEAD
    .venv/bin/python scripts/compare_screens.py --base main --head work
    .venv/bin/python scripts/compare_screens.py --keep shots/   # сохранить снимки

Выход 0 — на всех экранах 0 различающихся пикселей. Сдвиг цвета на 1-2
единицы (сглаживание градиента) за разницу не считается; экран снимается до
трёх раз с перерисовкой между съёмками, голова судится только по съёмкам, где
контроль совпал с базой, и разница засчитывается, только если держится во
всех них. Нестабильный экран тоже даёт выход 1.

Чего скрипт не видит: всё, что ниже первого экрана (снимок — видимая часть
окна; на ноутбуке разделы прокручиваются внутри себя), и то, что зависит от
действий на сервере: контроль и база ходят в одну службу. Список экранов
берётся из ``tests/test_ui.py`` рабочей копии, а не из сравниваемых ревизий.
Живую службу, фонотеку
и рабочую копию не трогает; нужны ffmpeg и Chromium Playwright.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TOKEN = "compare-screens-token"
SIZES = {"laptop": (1280, 800), "phone": (390, 844)}
FIXED_TIME = "2026-01-15T12:00:00"

# Одинаковая «случайность» в обеих страницах.
SEEDED_RANDOM = """
(() => {
    let s = 0x2545F491;
    Math.random = () => {
        s = (s + 0x6D2B79F5) | 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
})();
"""
# Без движения и без размытия под полупрозрачными панелями: размытие
# дорисовывается по-разному от кадра к кадру, а на вид от удаляемого CSS не зависит.
STILL = """
*, *::before, *::after {
    transition: none !important; animation: none !important; caret-color: transparent !important;
    backdrop-filter: none !important; -webkit-backdrop-filter: none !important;
}
"""
# Живое состояние, а не вёрстка: значок связи с сервером опрашивается по таймеру.
LIVE = ["#statusPill"]
# Снимки сравниваются в самом Chromium: Pillow в окружении нет.
DIFF_JS = """
async ([a, b]) => {
    const load = (src) => new Promise((ok, fail) => {
        const img = new Image(); img.onload = () => ok(img); img.onerror = fail; img.src = src;
    });
    const [x, y] = await Promise.all([load(a), load(b)]);
    if (x.width !== y.width || x.height !== y.height)
        return {size: [x.width, x.height, y.width, y.height], pixels: -1};
    const pixels = (img) => {
        const c = new OffscreenCanvas(img.width, img.height);
        const g = c.getContext("2d");
        g.drawImage(img, 0, 0);
        return g.getImageData(0, 0, img.width, img.height).data;
    };
    const p = pixels(x), q = pixels(y);
    let n = 0, box = null;
    for (let i = 0; i < p.length; i += 4) {
        // Сдвиг на 1-2 единицы — сглаживание градиента, его глазом не видно.
        if (Math.abs(p[i] - q[i]) <= 2 && Math.abs(p[i + 1] - q[i + 1]) <= 2
            && Math.abs(p[i + 2] - q[i + 2]) <= 2 && Math.abs(p[i + 3] - q[i + 3]) <= 2)
            continue;
        n++;
        const px = (i / 4) % x.width, py = Math.floor(i / 4 / x.width);
        box = box || [px, py, px, py];
        box = [Math.min(box[0], px), Math.min(box[1], py),
               Math.max(box[2], px), Math.max(box[3], py)];
    }
    return {pixels: n, box};
}
"""


# --------------------------------------------------------------- служба копии


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def export_tree(rev: str, into: Path) -> Path:
    """Файлы ревизии ``rev`` в ``into`` — без worktree и без записи в .git."""
    data = subprocess.run(
        ["git", "-C", str(PROJECT), "archive", "--format=tar", rev],
        check=True,
        capture_output=True,
    ).stdout
    into.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(into, filter="data")
    return into


def serve(tree: Path, port: int, tmp: Path) -> None:
    """Запускается в отдельном процессе: служба из ``tree`` на заглушках."""
    os.environ["API_TOKEN"] = TOKEN
    sys.path.insert(0, str(tree))
    import random

    import uvicorn

    from adder import (
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

    assert Path(runtime.__file__).is_relative_to(tree), runtime.__file__
    random.seed(0)
    root = tmp / "library"
    config.API_TOKEN = TOKEN
    config.LIBRARY = root
    config.MAX_WORKERS = 0
    config.DESKTOP_NOTIFICATIONS = False
    config.LISTENBRAINZ_TOKEN = ""
    for name in (
        "DB_PATH",
        "TMP_DIR",
        "TRASH_DIR",
        "OUTSIDE_DIR",
        "THUMB_DIR",
        "CACHE_DIR",
        "WEB_COVERS_DIR",
        "ARTIST_PHOTOS_DIR",
        "ARTIST_IMPORT_FILE",
    ):
        setattr(runtime, name, tmp / name.lower())
    playlists.HISTORY_DIR = tmp / "history"
    covers.COVERS_DIR = tmp / "covers"
    lyrics.CACHE_DIR = tmp / "lyrics"
    # Всё, что при старте или на экранах пошло бы в сеть или в другой процесс.
    stubs = [
        (navidrome, "configured", lambda: False),
        (sync, "start", lambda: None),
        (lyrics, "start_backfill", lambda: None),
        (lyrics, "_ask", lambda *a, **k: {}),
        (lyrics, "_search", lambda *a, **k: {}),
        (lyrics, "candidates", lambda *a, **k: []),
        (listenbrainz, "start", lambda: None),
        (outside, "candidates", lambda *a, **k: []),
        (analysis, "analyse_track", lambda path: None),
        (similar, "_ask", lambda *a, **k: None),
        (similar, "_ask_top", lambda *a, **k: None),
        (similar, "_ask_picture", lambda *a, **k: None),
        (discography, "start", lambda queue_one: None),
        (enrich, "lookup", lambda a, t: None),
        (enrich, "musicbrainz_lookup", lambda a, t: None),
        (ingest, "check_dependencies", lambda: {"ffmpeg": "ok", "js_runtime": "ok"}),
    ]
    for module, name, stub in stubs:
        setattr(module, name, stub)

    def track(rel: str, artist: str, title: str, seconds: int, gain: float | None, at: int):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"sine=frequency=440:duration={seconds}", "-ac", "1", "-b:a", "32k", str(path)],
            check=True,
        )  # fmt: skip
        ingest.write_tags(path, enrich.TrackInfo(album=title, artists=[artist]), title, None, None)
        if gain is not None:
            loudness.write(path, gain, 0.5)
        # «Свежие сверху» идут по времени файла: созданные в одну секунду треки
        # встали бы в разном порядке у двух служб.
        os.utime(path, (at, at))

    # Те же треки, что у tests/test_ui.py: обход открывает экраны по их именам.
    track("Loud Band/Singles/Loud.opus", "Loud Band", "Loud", 12, -6.0, at=1_760_000_000)
    track("Quiet/Singles/Quiet.m4a", "Quiet", "Quiet", 12, None, at=1_760_003_600)
    track("Evil/Singles/evil.m4a", "Evil", "Evil", 2, None, at=1_760_007_200)
    (root / "Обход.m3u").write_text(
        "Loud Band/Singles/Loud.opus\nQuiet/Singles/Quiet.m4a\n", encoding="utf-8"
    )
    library.invalidate_library_index()

    from adder import app as app_module

    uvicorn.run(app_module.app, host="127.0.0.1", port=port, log_level="warning")


def start_service(tree: Path, tmp: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    tmp.mkdir(parents=True)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve", str(tree), str(port), str(tmp)],
        cwd=tree,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"the service from {tree.name} exited ({process.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return process, url
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise SystemExit(f"the service from {tree.name} did not start")


# ------------------------------------------------------------------- экраны


def load_screens():
    """SWEEP_SCREENS и их открывалки из tests/test_ui.py этой рабочей копии."""
    os.environ.setdefault("API_TOKEN", TOKEN)
    sys.path.insert(0, str(PROJECT))
    spec = importlib.util.spec_from_file_location("ui_sweep", PROJECT / "tests" / "test_ui.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def open_page(browser, url: str, size: tuple[int, int], ui):
    context = browser.new_context(
        viewport={"width": size[0], "height": size[1]},
        reduced_motion="reduce",
        bypass_csp=True,  # для стиля «без анимаций» ниже
        device_scale_factor=1,
    )
    context.clock.set_fixed_time(FIXED_TIME)
    context.add_init_script(f"localStorage.setItem('token', '{TOKEN}');")
    context.add_init_script(SEEDED_RANDOM)
    page = context.new_page()
    page.wait_for_function = ui.csp_safe_wait(page)
    page.goto(url + "/")
    page.add_style_tag(content=STILL)
    page.wait_for_function("typeof switchView === 'function'")
    page.evaluate("playlists()")
    page.wait_for_function("knownPlaylists.some(p => p.name === 'Обход')")
    return page


def settle(page, wait_ms: int = 400) -> None:
    """Звук на ноль, шрифты загружены, картинки дорисованы."""
    page.evaluate(
        """async () => {
            if (window.player && player.audio) {
                player.audio.pause();
                player.audio.currentTime = 0;
            }
            await document.fonts.ready;
            await Promise.all([...document.images].filter(i => !i.complete)
                .map(i => new Promise(r => { i.onload = i.onerror = r; })));
        }"""
    )
    page.wait_for_timeout(wait_ms)


def compare(base_url: str, head_url: str, keep: Path | None) -> list[str]:
    from playwright.sync_api import sync_playwright

    ui = load_screens()
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        try:
            for size_name, size in SIZES.items():
                pages = {
                    "base": open_page(browser, base_url, size, ui),
                    "control": open_page(browser, base_url, size, ui),
                    "head": open_page(browser, head_url, size, ui),
                }
                differ = pages["base"]
                for index, (screen, open_screen, _scope) in enumerate(ui.SWEEP_SCREENS):
                    for page in pages.values():
                        open_screen(page)
                    # До трёх съёмок. Первая — не раньше секунды: то, что приходит
                    # вторым запросом, должно успеть. Между съёмками страница
                    # перерисовывается заново (окно на пиксель шире и обратно):
                    # пара точек сглаживания мелькает то в одной копии, то в
                    # другой, а простое ожидание кадр не перерисовывает. Разница
                    # головы засчитывается только по съёмкам, где контроль
                    # совпал с базой, и только если она держится во всех них:
                    # настоящая разница в вёрстке не пропадает от перерисовки.
                    noise = change = None
                    for attempt, wait_ms in enumerate((1000, 1000, 2000)):
                        if attempt:
                            for page in pages.values():
                                page.set_viewport_size({"width": size[0] + 1, "height": size[1]})
                                page.set_viewport_size({"width": size[0], "height": size[1]})
                        shots = {}
                        for name, page in pages.items():
                            settle(page, wait_ms)
                            shots[name] = page.screenshot(
                                animations="disabled",
                                caret="hide",
                                mask=[page.locator(s) for s in LIVE],
                            )
                        urls = {k: "data:image/png;base64," + _b64(v) for k, v in shots.items()}
                        here = differ.evaluate(DIFF_JS, [urls["base"], urls["control"]])
                        if noise is None or here["pixels"] < noise["pixels"]:
                            noise = here
                        if here["pixels"]:
                            continue  # страница не устоялась — голову по ней не судим
                        moved = differ.evaluate(DIFF_JS, [urls["base"], urls["head"]])
                        if change is None or moved["pixels"] < change["pixels"]:
                            change = moved
                            if keep:
                                for name, shot in shots.items():
                                    (keep / f"{size_name}-{index:02d}-{name}.png").write_bytes(shot)
                        if not change["pixels"]:
                            break
                    assert noise is not None
                    label = f"{size_name:6} {index:02d} {screen}"
                    if change is None:
                        problems.append(f"{label}: unstable (control differs by {noise})")
                        print(f"{label}: control {noise['pixels']} px, head not judged")
                        continue
                    if change["pixels"]:
                        problems.append(f"{label}: {change['pixels']} px differ ({change})")
                    print(f"{label}: control 0 px, head {change['pixels']} px")
                for page in pages.values():
                    page.context.close()
        finally:
            browser.close()
    return problems


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--base", default="HEAD~", help="ревизия «до» (по умолчанию HEAD~)")
    parser.add_argument("--head", default="HEAD", help="ревизия «после» (по умолчанию HEAD)")
    parser.add_argument("--keep", type=Path, help="сохранить снимки в эту папку")
    parser.add_argument("--serve", nargs=3, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve:
        tree, port, tmp = args.serve
        serve(Path(tree), int(port), Path(tmp))
        return 0

    if args.keep:
        args.keep.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="compare-screens-"))
    services: list[subprocess.Popen] = []
    try:
        urls = []
        for name, rev in (("base", args.base), ("head", args.head)):
            tree = export_tree(rev, work / name / "tree")
            process, url = start_service(tree, work / name / "state")
            services.append(process)
            urls.append(url)
        print(f"base {args.base} -> {urls[0]}, head {args.head} -> {urls[1]}")
        problems = compare(urls[0], urls[1], args.keep)
    finally:
        for process in services:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(work, ignore_errors=True)
    for problem in problems:
        print(f"DIFF: {problem}")
    print("screens: identical" if not problems else f"screens: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
