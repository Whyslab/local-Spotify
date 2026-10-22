"""Что слушали: полка «Недавно слушали» на главной и экран «Статистика».

Всё считается из журнала прослушиваний (таблица ``plays``, пишет
POST /api/plays) и индекса фонотеки — для названий и артистов. Пропущенное
не считается нигде: трек, выключенный на третьей секунде, не «слушали».
"""

import datetime
from typing import Any, Literal

from . import db

Period = Literal["month", "year", "all"]

# Прослушанным считается то, что не пропущено и звучало хотя бы пять секунд.
# Плеер ставит skipped, если трек выключили раньше 30 с, но старые клиенты и
# очередь без сети могли прислать нулевую длительность без пометки.
MIN_HEARD_SECONDS = 5
HEARD = "skipped = 0 AND played_seconds >= ?"

TOP = 10


def main_artist(row: dict) -> str:
    """Тот же артист, по которому открывается его страница (mainArtist в views.js)."""
    name = (row.get("albumartist") or row.get("artist") or "").split(" • ")[0].strip()
    return name or "Без артиста"


def recently_heard(by_path: dict[str, dict], limit: int = 20) -> list[str]:
    """Пути недавно прослушанных треков, свежие первыми, каждый по разу.

    Удалённые с тех пор файлы пропускаются — играть их нечем. Берём с запасом:
    после чистки фонотеки свежие строки журнала могут быть сплошь про
    удалённое.
    """
    rows = db.db_query(
        f"SELECT path, MAX(played_at) AS last, MAX(id) AS last_id FROM plays WHERE {HEARD} "
        "GROUP BY path ORDER BY last DESC, last_id DESC LIMIT 500",
        (MIN_HEARD_SECONDS,),
    )
    out = []
    for row in rows:
        if row["path"] in by_path:
            out.append(row["path"])
            if len(out) >= limit:
                break
    return out


def _month_back(day: datetime.date, months: int) -> datetime.date:
    """Первое число месяца, отстоящего от ``day`` на ``months`` назад."""
    index = day.year * 12 + day.month - 1 - months
    return datetime.date(index // 12, index % 12 + 1, 1)


def _months(first: datetime.date, last: datetime.date) -> list[str]:
    out = []
    index = first.year * 12 + first.month - 1
    end = last.year * 12 + last.month - 1
    while index <= end:
        out.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
        index += 1
    return out


def summary(period: Period, rows: list[dict], today: datetime.date) -> dict[str, Any]:
    """Время, число прослушиваний, первые десять артистов и треков, столбики.

    «Месяц» — последние 30 дней с сегодняшним (столбик на день), «год» —
    12 месяцев с текущим (столбик на месяц), «всё время» — помесячно с
    первого прослушивания. Скользящее окно, а не календарное: первого числа
    календарный месяц был бы пуст.

    played_at хранится местным временем через пробел ('2026-10-20 13:05:00'),
    поэтому граница — та же строка: 'ГГГГ-ММ-ДД' сравнивается с ней как текст
    и стоит раньше любого времени того же дня.
    """
    if period == "month":
        since: datetime.date | None = today - datetime.timedelta(days=29)
    elif period == "year":
        since = _month_back(today, 11)
    else:
        since = None
    where = HEARD + (" AND played_at >= ?" if since else "")
    params: tuple = (MIN_HEARD_SECONDS, since.isoformat()) if since else (MIN_HEARD_SECONDS,)

    total = db.db_query(
        f"SELECT COUNT(*) AS plays, COALESCE(SUM(played_seconds), 0) AS seconds, "
        f"MIN(played_at) AS first FROM plays WHERE {where}",
        params,
    )[0]

    by_path = {row["path"]: row for row in rows}
    tracks: list[dict] = []
    artists: dict[str, dict] = {}
    for row in db.db_query(
        f"SELECT path, COUNT(*) AS plays, SUM(played_seconds) AS seconds "
        f"FROM plays WHERE {where} GROUP BY path ORDER BY plays DESC, seconds DESC, path",
        params,
    ):
        tag = by_path.get(row["path"])
        if not tag:  # удалён с тех пор: время в итоге есть, в первых десяти — нет
            continue
        seconds = round(row["seconds"] or 0, 1)
        tracks.append(
            {
                **{k: tag.get(k) for k in ("path", "artist", "title", "album", "duration")},
                "plays": row["plays"],
                "seconds": seconds,
            }
        )
        name = main_artist(tag)
        # Обложка артиста — его самый слушаемый трек (строки идут по убыванию).
        who = artists.setdefault(
            name, {"name": name, "plays": 0, "seconds": 0.0, "cover": tag["path"]}
        )
        who["plays"] += row["plays"]
        who["seconds"] = round(who["seconds"] + seconds, 1)

    if period == "month":
        unit = "day"
        assert since is not None
        keys = [(since + datetime.timedelta(days=i)).isoformat() for i in range(30)]
        cut = 10
    else:
        unit = "month"
        if since is not None:
            first = since
        elif total["first"]:
            first = datetime.date.fromisoformat(total["first"][:10])
        else:
            first = today
        keys = _months(first, today)
        cut = 7
    bars = dict.fromkeys(keys, 0.0)
    for row in db.db_query(
        f"SELECT substr(played_at, 1, {cut}) AS key, SUM(played_seconds) AS seconds "
        f"FROM plays WHERE {where} GROUP BY key",
        params,
    ):
        if row["key"] in bars:
            bars[row["key"]] = round(row["seconds"] or 0, 1)

    return {
        "period": period,
        "since": since.isoformat() if since else None,
        "plays": total["plays"] or 0,
        "seconds": round(total["seconds"] or 0, 1),
        "artists": sorted(artists.values(), key=lambda a: (-a["plays"], -a["seconds"]))[:TOP],
        "tracks": tracks[:TOP],
        "chart": {"unit": unit, "bars": [{"key": k, "seconds": v} for k, v in bars.items()]},
    }
