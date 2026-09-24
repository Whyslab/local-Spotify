#!/usr/bin/env python3
"""Выгрузить полки главной страницы в подборки `.m3u`.

Полки («В машину», «Разогнаться», «На работу», «Поярче», «Может понравиться»)
считаются на лету, и на телефоне их нет вовсе: Amperfy видит только то, что
Navidrome нашёл в фонотеке, а там подборка — это файл. Скрипт пересобирает те
же пять полок в файлы `★ ....m3u` в корне фонотеки, и тогда их можно слушать
без панели.

Запускать ночью таймером (deploy/music-shelves.timer.template), и позже
ночного разбора звука: полки строятся из `audio_features`, поэтому запуск до
анализа собрал бы их по вчерашним измерениям.

Файлы перезаписываются целиком. Своя правка внутри `★ ...` не живёт — прежнее
содержимое уходит в adder/playlist-history, но постоянной подборке лучше дать
своё имя, без звезды.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    # adder.config намеренно не импортируется без токена: сервис слушает в сети
    # и пустой токен должен падать, а не тихо открывать доступ. Здесь наружу
    # ничего не слушается, поэтому значение любое.
    os.environ.setdefault("API_TOKEN", "shelves")
    try:
        from adder import shelves
    except ModuleNotFoundError as exc:
        # Всё нужное стоит в виртуальном окружении проекта, а в системном
        # python этого нет. Трассировка про fastapi об этом не скажет —
        # скажет интерпретатор, которым запустили.
        raise SystemExit(
            f"{exc.name} не установлен для {sys.executable}.\n"
            f"Запускать интерпретатором проекта:\n"
            f"    {REPO / '.venv/bin/python'} "
            f"{Path(__file__).resolve().relative_to(REPO)}\n"
            f"См. scripts/README.md."
        ) from exc

    report = shelves.export()
    if not report:
        print(
            "Ни одной полки не вышло: нечего выгружать. "
            "Похоже, треки ещё не измерены — сперва scripts/analyze_audio.py.",
            flush=True,
        )
        return 1

    written = 0
    for item in report:
        mark = "✓" if item["written"] else "•"
        print(f"  {mark} {item['name']}: {item['reason']}", flush=True)
        written += 1 if item["written"] else 0

    print(f"Итог: записано подборок {written}, пропущено {len(report) - written}", flush=True)
    # Ни одной записанной полки — это уже неисправность, а не «просто нечего
    # показывать»: таймер должен пожаловаться в журнал, иначе подборки будут
    # молча стареть.
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
