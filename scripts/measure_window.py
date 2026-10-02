#!/usr/bin/env python3
"""Как быстро открывается и листается окно плеера на ноутбуке (WebKitGTK).

Окно — это desktop/local-spotify.py, и меряется оно целиком: от запуска
процесса (Python, GTK, WebKit) до метки «app-ready» на странице. Окно
запускается в режиме замера (LOCAL_SPOTIFY_PERF=1), печатает строку
«PERF {…}» и закрывается само.

* **с нуля** — каждый запуск с новым пустым профилем WebKit (временные
  XDG_DATA_HOME и XDG_CACHE_HOME: ни кэша, ни service worker, ни
  localStorage), цель ≤ 1.5 с;
* **повторно** — тот же временный профиль, уже прогретый, цель ≤ 0.5 с;
* **прокрутка** — вся фонотека, разрывы кадров длиннее 50 мс, цель — ни одного.

Настоящий профиль окна и скачанное в нём не трогаются никогда. На экран окна
не выходят: замер поднимает свой виртуальный X-экран (Xvfb, пакет
xorg-server-xvfb) и запускает окна на нём; без Xvfb скрипт отказывается, а не
открывает окна на рабочем столе. Цифры на Xvfb — программная отрисовка,
то есть скорее хуже настоящего окна, чем лучше. Окно запускается системным
Python: в .venv нет gi.

    .venv/bin/python scripts/measure_window.py              # 10 + 10 запусков и прокрутка
    .venv/bin/python scripts/measure_window.py --budget     # выход 1, если цель не выполнена
    .venv/bin/python scripts/measure_window.py --smoke      # окно дошло до «готово» без ошибок
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_ui import misses, read_token, stats, table  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
SHELL = PROJECT / "desktop" / "local-spotify.py"
SYSTEM_PYTHON = "/usr/bin/python3"
SCREEN = "1280x800x24"


def start_xvfb() -> tuple[subprocess.Popen, str]:
    """Свой виртуальный экран на весь замер; номер выбирает сам Xvfb."""
    if not shutil.which("Xvfb"):
        raise SystemExit("нужен Xvfb (pacman -S xorg-server-xvfb): окна на рабочий стол не выводим")
    read, write = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write), "-screen", "0", SCREEN, "-nolisten", "tcp"],
        pass_fds=(write,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write)
    # Xvfb пишет номер экрана, когда готов; повис — не ждать вечно.
    ready, _, _ = select.select([read], [], [], 15)
    with os.fdopen(read) as pipe:
        number = pipe.readline().strip() if ready else ""
    if not number:
        proc.kill()
        raise SystemExit("Xvfb не запустился")
    return proc, ":" + number


def parse_perf(lines: list[str]) -> list[dict]:
    """Строки «PERF {…}» из вывода окна; прочее — мимо."""
    out = []
    for line in lines:
        if line.startswith("PERF "):
            try:
                out.append(json.loads(line[5:]))
            except json.JSONDecodeError:
                continue
    return out


def launch(
    profile: Path, token: str, display: str, scroll: bool = False, timeout: float = 60
) -> tuple[float, list[dict]]:
    """Один запуск окна. Возвращает (мс от запуска до «готово», события)."""
    env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    env.update(
        DISPLAY=display,
        GDK_BACKEND="x11",
        LOCAL_SPOTIFY_PERF="1",
        LOCAL_SPOTIFY_PERF_EXIT="1",
        LOCAL_SPOTIFY_PERF_SCROLL="1" if scroll else "0",
        XDG_DATA_HOME=str(profile / "data"),
        XDG_CACHE_HOME=str(profile / "cache"),
        LC_ALL="C",
        LOCAL_SPOTIFY_TOKEN=token,
    )
    started = time.time() * 1000
    proc = subprocess.run(
        [SYSTEM_PYTHON, str(SHELL)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    events = parse_perf(proc.stdout.splitlines())
    ready = next((e for e in events if e.get("event") == "ready"), None)
    if ready is None:
        raise RuntimeError(f"окно не дошло до «готово»: {proc.stderr.strip()[-400:]}")
    return ready["wall_ms"] - started, events


def errors_of(events: list[dict]) -> list[str]:
    return [e.get("message", "?") for e in events if e.get("event") == "error"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--budget", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--env", type=Path, default=PROJECT / "adder" / ".env")
    args = parser.parse_args(argv)

    token = read_token(args.env)
    xvfb, display = start_xvfb()
    try:
        return measure(args, token, display)
    finally:
        xvfb.terminate()
        xvfb.wait(timeout=10)


def measure(args, token: str, display: str) -> int:
    with tempfile.TemporaryDirectory(prefix="ls-window-") as tmp:
        root = Path(tmp)
        if args.smoke:
            try:
                ms, events = launch(root / "smoke", token, display)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(f"smoke: {exc}")
                return 1
            problems = errors_of(events)
            for p in problems:
                print("error:", p)
            print(f"smoke: {'ok' if not problems else 'ошибки'} (готово за {ms:.0f} мс)")
            return 1 if problems else 0

        # Запуск, который не дошёл до «готово», — промах (None), а не повод
        # потерять все остальные цифры.
        def attempt(profile: Path, **kw) -> tuple[float | None, list[dict]]:
            try:
                return launch(profile, token, display, **kw)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(f"запуск не удался: {exc}", file=sys.stderr)
                return None, []

        cold = [attempt(root / f"cold-{i}")[0] for i in range(args.runs)]
        warm_profile = root / "warm"
        attempt(warm_profile)  # прогрев: кэш, service worker, localStorage
        warm = [attempt(warm_profile)[0] for _ in range(args.runs)]
        _, events = attempt(warm_profile, scroll=True, timeout=180)

    scroll = next((e for e in events if e.get("event") == "scroll"), {})
    gaps = scroll.get("frame_gaps_over_50ms")
    results = {
        "window": {
            "ready_cold": stats(cold),
            "ready_warm": stats(warm),
            "long_tasks": stats([] if gaps is None else [float(gaps)]),
        }
    }
    print(table(results))
    if scroll:
        longest = scroll.get("longest_gap_ms", 0)
        print(f"прокрутка: строк {scroll.get('rows')}, самый долгий кадр {longest:.0f} мс")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": results, "scroll": scroll}
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    missed = misses(results)
    if missed:
        print("\nцели не выполнены:\n  " + "\n  ".join(missed))
    return 1 if (args.budget and missed) else 0


if __name__ == "__main__":
    sys.exit(main())
