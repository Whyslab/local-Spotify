"""Полки главной страницы — и они же файлами подборок.

Полки («В машину», «Разогнаться», «На работу», «Поярче», «Может понравиться»)
считаются на лету из `audio_features` и индекса фонотеки. На телефоне их нет:
Amperfy показывает только то, что Navidrome нашёл в фонотеке, а там подборка —
это файл `.m3u`. Поэтому те же полки раз в сутки записываются в файлы, и тогда
их можно слушать без панели.

Полки считаются в одном месте. `discover` живёт здесь, а `adder.app` берёт её
отсюда: будь две реализации, главная и подборки со временем разошлись бы, и
«Может понравиться» на телефоне показывало бы не то, что на ноутбуке.
"""

from __future__ import annotations

import logging
import random
from collections import Counter

from . import db, library, moods, playlists, runtime, similar

logger = logging.getLogger(__name__)

# Приставка отличает выгруженную полку от своей подборки: она сортируется
# первой и видно сразу, что файл пересобирается сам и правки в нём не живут.
PREFIX = "★ "

DISCOVER_NAME = "Может понравиться"

# Столько же, сколько требует moods.pick: меньше восьми треков — это не
# подборка. Такую полку не пишем вовсе, и вчерашний файл остаётся на месте:
# вчерашняя подборка лучше пустой.
MIN_TRACKS = 8


def discover(rows: list[dict], top: int = 6, want: int = 24) -> dict:
    """Треки своей же фонотеки, о которых не думал.

    «Предпочтения» берутся из состава фонотеки, а не из истории прослушиваний:
    в `plays` сейчас четыре десятка записей, и почти все — проверочные включения
    по секунде. Собранное своими руками — свидетельство вкуса надёжнее, чем
    журнал, которого пока нет.

    Дальше Deezer называет похожих на самых собранных артистов, и из фонотеки
    отбирается то, что этими похожими написано, — за вычетом самих любимцев:
    их треки не открытие.
    """
    counted = Counter(similar.primary(row.get("artist") or "") for row in rows if row.get("artist"))
    counted.pop("", None)
    favourites = [name for name, _ in counted.most_common(top)]
    if not favourites:
        return {"based_on": [], "tracks": []}

    cache = runtime.PROJECT / "similar-cache"
    neighbours: set[str] = set()
    for name in favourites:
        neighbours.update(similar.similar_artists(name, cache, limit=10))
    neighbours -= {name.lower() for name in favourites}

    lowered = {name.lower() for name in neighbours}
    picked = [
        row
        for row in rows
        if similar.primary(row.get("artist") or "").lower() in lowered
        and similar.primary(row.get("artist") or "") not in favourites
    ]
    random.Random(len(picked)).shuffle(picked)
    return {
        "based_on": favourites,
        "tracks": [
            {k: row.get(k) for k in ("path", "artist", "title", "album", "duration")}
            for row in picked[:want]
        ],
    }


def export(limit: int = 40) -> list[dict]:
    """Записать полки главной в подборки `.m3u` и рассказать, что вышло.

    Данные те же, что отдаёт `GET /api/home`: индекс фонотеки и измерения. Так
    выгруженная подборка совпадает с тем, что видно в панели, — иначе полка на
    телефоне и полка на ноутбуке назывались бы одинаково, а звучали по-разному.
    """
    rows = library.library_index()
    features = db.db_query("SELECT path, tempo, energy, brightness FROM audio_features")

    # Пути и в `audio_features`, и в индексе — относительные от корня фонотеки
    # (analyze_audio пишет `path.relative_to(root)`), то есть в той же форме,
    # какую ждёт playlists.render. Но в измерениях остаются и удалённые треки:
    # трек уходит из фонотеки, а его строка в таблице живёт. Главная их не
    # показывает — `as_tracks` в app.py берёт только то, что есть в индексе, —
    # и подборка должна вести себя так же, иначе в неё попадут битые строки.
    known = {row["path"] for row in rows}

    shelves = [(mood.name, mood.paths) for mood in moods.collections(features, limit=limit)]
    shelves.append((DISCOVER_NAME, [track["path"] for track in discover(rows)["tracks"]]))

    report = []
    for shelf, paths in shelves:
        # В отчёте — имя подборки, а не полки: это то, что лежит в фонотеке и
        # что человек увидит в Amperfy.
        name = f"{PREFIX}{shelf}"
        alive = [path for path in paths if path in known]
        if len(alive) < MIN_TRACKS:
            logger.info("Полка %r пропущена: треков %d", name, len(alive))
            report.append(
                {
                    "name": name,
                    "count": len(alive),
                    "written": False,
                    "reason": f"треков {len(alive)}, нужно хотя бы {MIN_TRACKS} — файл не тронут",
                }
            )
            continue
        # expected_revision=None намеренно: полка пересобирается целиком, и
        # прошлое содержимое файла нас не касается. Прежняя версия всё равно
        # уходит в playlist-history, так что затёртую правку можно достать.
        playlists.write(name, alive, expected_revision=None)
        report.append(
            {
                "name": name,
                "count": len(alive),
                "written": True,
                "reason": f"записано треков: {len(alive)}",
            }
        )
    return report
