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

# Окно метрики «повторов за 3 дня».
REPEAT_DAYS = 3


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
        "repeats": shuffle_repeats(),
        "smart_skips": smart_skips(),
        "period": period,
        "since": since.isoformat() if since else None,
        "plays": total["plays"] or 0,
        "seconds": round(total["seconds"] or 0, 1),
        "artists": sorted(artists.values(), key=lambda a: (-a["plays"], -a["seconds"]))[:TOP],
        "tracks": tracks[:TOP],
        "chart": {"unit": unit, "bars": [{"key": k, "seconds": v} for k, v in bars.items()]},
    }


def shuffle_repeats(days: int = REPEAT_DAYS) -> dict[str, Any]:
    """Сколько раз перемешивание за последние ``days`` дня вернуло трек,
    который за это окно уже звучал. Мерило колоды (adder/deck.py): до неё
    за 17–18.11 из 145 прослушиваний разных было 50.

    Считаются только треки из колоды (plays.deck) и повтор — внутри той же
    колоды. С 20.11.2026: раньше сюда шла и кнопка перемешивания в плеере
    (manual + shuffled) — у артиста или альбома она тасует без колоды, и
    артист, слушанный по кругу, выглядел ошибкой колоды. Тот же трек из двух
    разных колод (вся фонотека и подборка) — не её промах. Прослушивания до
    20.11 пометки не имеют и сюда не попадают.

    И только услышанное: ранний пропуск колода нарочно возвращает позже в
    круге, это не повтор. Маленькая подборка, прослушанная дважды за три дня,
    повторы даст честно — их там не избежать.
    """
    row = db.db_query(
        "SELECT COUNT(*) AS plays, COUNT(DISTINCT deck || char(10) || path) AS distinct_tracks"
        f" FROM plays WHERE deck IS NOT NULL AND {HEARD} "
        "AND played_at >= datetime('now', 'localtime', ?)",
        (MIN_HEARD_SECONDS, f"-{int(days)} days"),
    )[0]
    plays = row["plays"] or 0
    return {"days": days, "plays": plays, "repeats": plays - (row["distinct_tracks"] or 0)}


# Окно метрики пропусков в умном.
SMART_DAYS = 14


def smart_skips(days: int = SMART_DAYS, engine: str = "sonic") -> dict[str, Any]:
    """Доля пропусков в умном перемешивании и радио **вместе с чужими**
    треками (outside_plays) за ``days`` дней.

    Только свои треки — мерило нечестное: «своих пропускают меньше, чем в
    обычном» было правдой и до нового умного (разбор 19.11.2026). Чужие
    пропускают чаще, и без них доля выглядит лучше, чем звучит очередь.
    """
    since = f"-{int(days)} days"
    # Только очереди этого движка (plays.engine): прослушивания до метки и
    # старым движком сюда не попадают — иначе сравнивать было бы нечего.
    where = (
        "WHERE mode IN ('smart', 'radio') AND engine = ? "
        "AND played_at >= datetime('now', 'localtime', ?)"
    )
    own = db.db_query(
        f"SELECT COUNT(*) AS plays, COALESCE(SUM(skipped), 0) AS skips FROM plays {where}",
        (engine, since),
    )[0]
    other = db.db_query(
        "SELECT COUNT(*) AS plays, COALESCE(SUM(skipped), 0) AS skips FROM outside_plays " + where,
        (engine, since),
    )[0]
    plays = (own["plays"] or 0) + (other["plays"] or 0)
    skips = (own["skips"] or 0) + (other["skips"] or 0)
    # Мерка для сравнения — обычное перемешивание за те же дни (в разборе
    # 19.11.2026 у него было 24 % пропусков).
    plain = db.db_query(
        "SELECT COUNT(*) AS plays, COALESCE(SUM(skipped), 0) AS skips FROM plays "
        "WHERE (mode = 'plain' OR (mode = 'manual' AND shuffled = 1)) "
        "AND played_at >= datetime('now', 'localtime', ?)",
        (since,),
    )[0]
    return {
        "days": days,
        "engine": engine,
        "plain_plays": plain["plays"] or 0,
        "plain_share": round(plain["skips"] / plain["plays"], 3) if plain["plays"] else None,
        "plays": plays,
        "skips": skips,
        "outside_plays": other["plays"] or 0,
        "share": round(skips / plays, 3) if plays else None,
    }


# «Итоги года» — как Spotify Wrapped. Показываются в «Статистике» всегда, а
# на главной баннером с 1 декабря по 15 января (до 15 января — итоги прошлого
# года: в первые дни нового смотреть ещё не на что).
WRAPPED_TOP = 5
WRAPPED_PLAYLIST = 50
WRAPPED_LAST_JANUARY_DAY = 15
PARTS_OF_DAY = (("ночью", 0, 6), ("утром", 6, 12), ("днём", 12, 18), ("вечером", 18, 24))


def wrapped_year(today: datetime.date) -> int:
    """Чьи итоги сейчас: до 15 января — прошлого года."""
    if today.month == 1 and today.day <= WRAPPED_LAST_JANUARY_DAY:
        return today.year - 1
    return today.year


def _longest_streak(days: list[str]) -> dict[str, Any]:
    """Самая длинная серия дней подряд с музыкой (даты по возрастанию)."""
    best: dict[str, Any] = {"days": 0, "from": None, "to": None}
    prev: datetime.date | None = None
    run_from = datetime.date.min
    length = 0
    for key in days:
        day = datetime.date.fromisoformat(key)
        if prev is not None and day - prev == datetime.timedelta(days=1):
            length += 1
        else:
            run_from, length = day, 1
        prev = day
        if length > best["days"]:
            best = {"days": length, "from": run_from.isoformat(), "to": day.isoformat()}
    return best


def wrapped(year: int, rows: list[dict], today: datetime.date) -> dict[str, Any]:
    """Итоги ``year``: сколько, кто и что, когда — из журнала, без пропущенного.

    Удалённые с тех пор треки в общем времени есть, а в первых местах — нет
    (как у summary): включить их нечем.
    """
    start, end = f"{year:04d}-01-01", f"{year + 1:04d}-01-01"
    where = HEARD + " AND played_at >= ? AND played_at < ?"
    params = (MIN_HEARD_SECONDS, start, end)
    total = db.db_query(
        f"SELECT COUNT(*) AS plays, COALESCE(SUM(played_seconds), 0) AS seconds, "
        f"COUNT(DISTINCT path) AS tracks, MIN(played_at) AS first FROM plays WHERE {where}",
        params,
    )[0]
    out: dict[str, Any] = {
        "year": year,
        "complete": today.year > year,
        "plays": total["plays"] or 0,
        "seconds": round(total["seconds"] or 0, 1),
        "tracks_count": total["tracks"] or 0,
        "first": (total["first"] or "")[:10] or None,
    }
    if not out["plays"]:
        return out

    by_path = {row["path"]: row for row in rows}
    tracks: list[dict] = []
    artists: dict[str, dict] = {}
    for row in db.db_query(
        f"SELECT path, COUNT(*) AS plays, SUM(played_seconds) AS seconds "
        f"FROM plays WHERE {where} GROUP BY path ORDER BY plays DESC, seconds DESC, path",
        params,
    ):
        tag = by_path.get(row["path"])
        if not tag:
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
        who = artists.setdefault(
            name, {"name": name, "plays": 0, "seconds": 0.0, "cover": tag["path"], "tracks": 0}
        )
        who["plays"] += row["plays"]
        who["seconds"] = round(who["seconds"] + seconds, 1)
        who["tracks"] += 1
    ranked = sorted(artists.values(), key=lambda a: (-a["seconds"], -a["plays"]))
    out["artists_count"] = len(artists)
    out["top_artists"] = ranked[:WRAPPED_TOP]
    out["top_tracks"] = tracks[:WRAPPED_TOP]
    # «Слушать лучшее года» — первые пятьдесят, в порядке мест.
    out["playlist"] = [
        {k: t.get(k) for k in ("path", "artist", "title", "album", "duration")}
        for t in tracks[:WRAPPED_PLAYLIST]
    ]

    days = db.db_query(
        f"SELECT substr(played_at, 1, 10) AS day, SUM(played_seconds) AS seconds "
        f"FROM plays WHERE {where} GROUP BY day ORDER BY day",
        params,
    )
    top_day = max(days, key=lambda d: (d["seconds"] or 0, d["day"]))
    out["top_day"] = {"date": top_day["day"], "seconds": round(top_day["seconds"] or 0, 1)}
    out["days_count"] = len(days)
    out["streak"] = _longest_streak([d["day"] for d in days])

    months = dict.fromkeys(_months(datetime.date(year, 1, 1), datetime.date(year, 12, 1)), 0.0)
    for d in days:
        months[d["day"][:7]] = round(months.get(d["day"][:7], 0.0) + (d["seconds"] or 0), 1)
    out["months"] = [{"key": k, "seconds": v} for k, v in months.items()]

    hours = db.db_query(
        f"SELECT CAST(substr(played_at, 12, 2) AS INTEGER) AS hour, SUM(played_seconds) AS seconds "
        f"FROM plays WHERE {where} GROUP BY hour",
        params,
    )
    parts = {name: 0.0 for name, _, _ in PARTS_OF_DAY}
    for h in hours:
        for name, lo, hi in PARTS_OF_DAY:
            if h["hour"] is not None and lo <= h["hour"] < hi:
                parts[name] += h["seconds"] or 0
    whole = sum(parts.values()) or 1.0
    out["parts_of_day"] = [
        {"name": name, "share": round(value / whole, 3)} for name, value in parts.items()
    ]
    out["top_part"] = max(parts, key=lambda name: parts[name])

    # «На повторе»: трек, сыгранный больше всего раз за один день.
    repeat = db.db_query(
        f"SELECT path, substr(played_at, 1, 10) AS day, COUNT(*) AS plays FROM plays "
        f"WHERE {where} GROUP BY path, day ORDER BY plays DESC, day DESC LIMIT 20",
        params,
    )
    for row in repeat:
        tag = by_path.get(row["path"])
        if tag and row["plays"] >= 3:
            out["on_repeat"] = {
                **{k: tag.get(k) for k in ("path", "artist", "title")},
                "plays": row["plays"],
                "date": row["day"],
            }
            break
    return out
