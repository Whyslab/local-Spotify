"""Колода обычного перемешивания: одна перестановка на подборку и на фонотеку.

Раньше каждое «Перемешать» тасовало набор заново, и за неделю одни треки
звучали по три раза, а другие ни разу (разбор 19.11.2026). Колода — как у
карт: перестановка всего набора хранится в базе и проходится до конца, а
новая сессия, с какого бы устройства она ни началась, продолжает с того
места, где остановилась прошлая.

Что где:

* Курсор двигают **прослушивания** (POST /api/plays → ``heard``), а не выдача:
  выданное, но не сыгранное остаётся в колоде. Прослушивание без сети приходит
  позже со своим heard_at и засчитывается тогда же. Любое прослушивание трека
  (из колоды, из другой подборки, включённое руками) отмечает его во всех
  колодах, где он есть, — решение владельца 19.11.2026.
* Пропуск в первые 30 с — не «сыграно»: трек уезжает во вторую половину
  оставшейся части. Второй ранний пропуск за круг — уже сыгран.
* Два устройства сразу берут из одной колоды. Чтобы они не играли одно и то же,
  первые ``LEASE_SIZE`` выданных устройству треков на время брони не выдаются
  другим. Бронь — в памяти: перезапуск службы её снимает, а колода остаётся.
* Новая колода собирается с балансом по артисту и, внутри артиста, по альбому
  (Spotify 2014 / Фидлер, «balanced shuffle»): треки одного артиста
  раскладываются по колоде равномерно со случайным сдвигом. Совместные треки
  («A • B») сторожат обоих: потом проход ``_spread_artists`` раздвигает соседей
  с общим артистом.
* На стыке колод последние ``JUNCTION`` треков старой не попадают в первые
  ``JUNCTION`` новой — иначе конец одного круга и начало другого звучали бы
  как повтор.
* Новые треки набора встают в случайное место оставшейся части; убранные
  исчезают. Повтор строки в подборке — один трек.
"""

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from . import db

logger = logging.getLogger(__name__)

LIBRARY_KEY = "library"

# Сколько треков с конца старой колоды не пускать в начало новой. В маленьком
# наборе — треть: при сорока треках запрет на сто означал бы «никогда».
JUNCTION = 100

# Пропуск раньше стольких секунд — «не сейчас», а не «сыграно». Как и в
# плеере (SKIP_THRESHOLD_SECONDS).
SKIP_SECONDS = 30

# Осталось меньше — следующая колода собирается заранее и отдаётся клиенту
# началом: плеер переходит в новый круг без паузы на сеть.
NEXT_WHEN_LEFT = 50
NEXT_SHOWN = 50

# Бронь для двух устройств: первые столько выданных и на столько секунд.
# Полчаса, а не дольше: один человек закрыл ноутбук и открыл телефон —
# новая сессия должна продолжить с курсора, а не с шестнадцатого трека.
# Играющее устройство продлевает бронь каждой сверкой (раз в 8 треков).
LEASE_SIZE = 15
LEASE_SECONDS = 30 * 60

# Дальше стольких треков артистов не раздвигаем: у артиста с половиной
# подборки всё равно не выйдет, а у редких хватает и этого.
MAX_GAP = 8
# Насколько далеко вперёд искать трек на замену при раздвигании.
SEARCH = 60

_LOCK = threading.RLock()
# key → device → (до какого времени, какие пути)
_LEASES: dict[str, dict[str, tuple[float, frozenset[str]]]] = {}


def playlist_key(name: str) -> str:
    return "playlist:" + name


def artists_of(row: dict) -> list[str]:
    """Артисты трека для баланса: «A • B» — оба, регистр и пробелы не важны."""
    raw = row.get("artist") or row.get("albumartist") or ""
    names = [" ".join(part.lower().split()) for part in raw.split("•")]
    return [name for name in names if name]


# ---------------------------------------------------------------------------
# Сборка колоды


Meta = dict[str, dict]


def _balanced(
    paths: list[str],
    key: Callable[[str], str],
    rng: random.Random,
    inner: Callable[[list[str]], list[str]] | None = None,
) -> list[str]:
    """Сбалансированное перемешивание (Фидлер): каждая группа — равномерно
    по отрезку [0, 1) со случайным началом и разбросом шага ±10 %; порядок
    внутри группы даёт ``inner`` (по альбому), иначе — случайный."""
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(key(path), []).append(path)
    placed: list[tuple[float, float, str]] = []
    for group in groups.values():
        seq = inner(group) if inner else rng.sample(group, len(group))
        step = 1.0 / len(seq)
        pos = rng.random() * step
        for path in seq:
            placed.append((pos, rng.random(), path))
            pos += step * rng.uniform(0.9, 1.1)
    placed.sort()
    return [path for _, _, path in placed]


def _gaps(order: list[str], meta: Meta) -> dict[str, int]:
    """Как далеко друг от друга держать треки каждого артиста: половина
    равномерного шага, но не больше MAX_GAP и не меньше 1 (то есть «без
    условий»)."""
    counts: dict[str, int] = {}
    for path in order:
        for name in artists_of(meta.get(path, {})):
            counts[name] = counts.get(name, 0) + 1
    n = len(order)
    return {name: max(1, min(MAX_GAP, n // (2 * c))) for name, c in counts.items()}


def _clashes(
    order: list[str], i: int, artists: list[str], gaps: dict[str, int], meta: Meta
) -> bool:
    """Встал бы трек с ``artists`` на место ``i`` ближе положенного к своему?"""
    for name in artists:
        gap = gaps.get(name, 1)
        for j in range(max(0, i - gap + 1), i):
            if name in artists_of(meta.get(order[j], {})):
                return True
    return False


def _spread_artists(
    order: list[str],
    meta: Meta,
    gaps: dict[str, int],
    frozen_head: int = 0,
    avoid: frozenset[str] = frozenset(),
) -> list[str]:
    """Раздвинуть треки одного артиста, которые баланс оставил рядом.

    В основном это совместные треки: баланс раскладывает их по первому
    артисту, второй оказывается где придётся. Трек, который встал слишком
    близко к своему, меняется местами с ближайшим следующим, которому здесь
    можно. Поздние места проверяются после ранних, так что сдвиг не ломает
    уже проверенное. ``avoid`` (хвост прошлой колоды) в первые
    ``frozen_head`` мест не переезжает.
    """
    out = list(order)
    for i in range(len(out)):
        if not _clashes(out, i, artists_of(meta.get(out[i], {})), gaps, meta):
            continue
        for j in range(i + 1, min(len(out), i + SEARCH)):
            candidate = out[j]
            if i < frozen_head and candidate in avoid:
                continue
            if not _clashes(out, i, artists_of(meta.get(candidate, {})), gaps, meta):
                out.insert(i, out.pop(j))
                break
        else:
            # Впереди замены нет (конец колоды) — обмен с более ранним треком,
            # если обоим на новых местах хорошо с обеих сторон.
            for k in range(i - 1, max(-1, i - SEARCH), -1):
                if k < frozen_head and out[i] in avoid:
                    continue
                out[k], out[i] = out[i], out[k]
                if _fits(out, k, gaps, meta) and _fits(out, i, gaps, meta):
                    break
                out[k], out[i] = out[i], out[k]
    return out


def _fits(order: list[str], pos: int, gaps: dict[str, int], meta: Meta) -> bool:
    """Трек на месте ``pos`` не ближе положенного к своим ни слева, ни справа."""
    for name in artists_of(meta.get(order[pos], {})):
        gap = gaps.get(name, 1)
        for j in range(max(0, pos - gap + 1), min(len(order), pos + gap)):
            if j != pos and name in artists_of(meta.get(order[j], {})):
                return False
    return True


def junction_size(n: int) -> int:
    return min(JUNCTION, n // 3)


def build(
    paths: Iterable[str], meta: Meta, rng: random.Random, avoid: Iterable[str] = ()
) -> list[str]:
    """Новая колода из ``paths``: баланс по артисту и альбому, ``avoid``
    (последние треки прошлой колоды) — не в первых ``junction_size`` местах."""
    unique = list(dict.fromkeys(paths))
    if not unique:
        return []

    def primary(path: str) -> str:
        names = artists_of(meta.get(path, {}))
        # Без артиста — сам по себе: «никто» не артист, которого надо раздвигать.
        return names[0] if names else "\0" + path

    def album_order(group: list[str]) -> list[str]:
        def album(path: str) -> str:
            name = " ".join(str(meta.get(path, {}).get("album") or "").lower().split())
            return name or "\0" + path

        return _balanced(group, album, rng)

    order = _balanced(unique, primary, rng, album_order)

    head = junction_size(len(order))
    blocked = frozenset(avoid) & set(order)
    if head and blocked:
        free = [p for p in order if p not in blocked]
        head = min(head, len(free))
        tail = free[head:]
        for path in (p for p in order if p in blocked):
            tail.insert(rng.randint(0, len(tail)), path)
        order = free[:head] + tail
    return _spread_artists(order, meta, _gaps(order, meta), head, blocked)


# ---------------------------------------------------------------------------
# Хранение


def _load(key: str) -> dict | None:
    rows = db.db_query("SELECT state FROM decks WHERE key = ?", (key,))
    if not rows:
        return None
    try:
        state = json.loads(rows[0]["state"])
    except (TypeError, ValueError):
        logger.warning("Deck %s: broken state, starting over", key, extra={"task_id": "deck"})
        return None
    return state if isinstance(state, dict) and isinstance(state.get("order"), list) else None


def _save(key: str, state: dict) -> None:
    db.db_exec(
        "INSERT INTO decks(key, state, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
        (key, json.dumps(state, ensure_ascii=False), time.time()),
    )


def _fresh(
    round_no: int,
    members: list[str],
    meta: Meta,
    rng: random.Random,
    avoid: Iterable[str],
    now: float,
) -> dict:
    return {
        "round": round_no,
        "started_at": now,
        "order": build(members, meta, rng, avoid),
        "heard": [],
        "moved": [],
        "next": None,
    }


def _remaining(state: dict) -> list[str]:
    heard = set(state["heard"])
    return [p for p in state["order"] if p not in heard]


def _roll(
    state: dict, members: list[str] | None, meta: Meta | None, rng: random.Random, now: float
) -> dict:
    """Следующий круг: собранная заранее колода или новая."""
    nxt = state.get("next")
    if nxt is None:
        assert members is not None and meta is not None
        tail = (state["heard"] + _remaining(state))[-junction_size(len(state["order"])) :]
        return _fresh(state["round"] + 1, members, meta, rng, tail, now)
    return {
        "round": state["round"] + 1,
        "started_at": now,
        "order": nxt,
        "heard": [],
        "moved": [],
        "next": None,
    }


def _insert_remaining(state: dict, path: str, rng: random.Random) -> None:
    """Поставить ``path`` в случайное место оставшейся (не сыгранной) части."""
    heard = set(state["heard"])
    slots = [i for i, p in enumerate(state["order"]) if p not in heard]
    k = rng.randint(0, len(slots))
    if k < len(slots):
        state["order"].insert(slots[k], path)
    else:
        state["order"].append(path)


# Фонотека вдруг меньше этой доли колоды — не верим ей: индекс собирается
# целиком и подменяется разом, так что такое бывает, когда диск не смонтирован
# или папка пуста. Убранное стёрло бы и сыгранное, а вернувшееся встало бы в
# колоду заново. Порог низкий: настоящая чистка фонотеки (даже вдвое) должна
# пройти, иначе колода застряла бы на удалённых треках.
SHRINK_GUARD = 0.1


def _members_for(key: str, state: dict, members: list[str]) -> list[str]:
    """Набор, по которому сверять колоду. Фонотека, вдруг сжавшаяся почти до
    нуля, — это несмонтированный диск, а не правда: тогда колода
    остаётся как есть. Подборку человек вправе сократить хоть до одного трека."""
    if key != LIBRARY_KEY or not state["order"]:
        return members
    if len(set(members) & set(state["order"])) >= SHRINK_GUARD * len(state["order"]):
        return members
    logger.warning(
        "Deck: library shrank from %d to %d tracks, not trusted this time",
        len(state["order"]),
        len(members),
        extra={"task_id": "deck"},
    )
    return list(state["order"])


def _sync(state: dict, members: list[str], rng: random.Random) -> bool:
    """Подогнать колоду под нынешний набор. True — что-то поменялось."""
    have = set(members)
    changed = False
    for field in ("order", "heard", "moved"):
        kept = [p for p in state[field] if p in have]
        if len(kept) != len(state[field]):
            state[field] = kept
            changed = True
    if state.get("next") is not None:
        kept = [p for p in state["next"] if p in have]
        if len(kept) != len(state["next"]):
            state["next"] = kept
            changed = True
    known = set(state["order"])
    for path in members:
        if path in known:
            continue
        _insert_remaining(state, path, rng)
        if state.get("next") is not None:
            # Не в начало следующей: в конце этой он уже может стоять.
            head = min(junction_size(len(state["next"]) + 1), len(state["next"]))
            state["next"].insert(rng.randint(head, len(state["next"])), path)
        known.add(path)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Выдача и прослушивания


def _leased_by_others(key: str, device: str, now: float) -> set[str]:
    out: set[str] = set()
    leases = _LEASES.get(key, {})
    for other, (until, paths) in list(leases.items()):
        if until <= now:
            del leases[other]
        elif other != device:
            out |= paths
    return out


def take(
    key: str,
    members: list[str],
    meta: Meta,
    device: str,
    only: Iterable[str] | None = None,
    now: float | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Что играть дальше: несыгранное этого круга по порядку колоды.

    ``only`` — только эти пути (перемешивание очереди, а не всего набора).
    Когда несыгранного меньше ``NEXT_WHEN_LEFT``, к ответу прикладывается
    начало следующей колоды (``next``) — её номер ``round + 1``.
    """
    now = time.time() if now is None else now
    rng = rng or random.Random()
    members = list(dict.fromkeys(members))
    with _LOCK:
        state = _load(key)
        changed = False
        if state is None:
            state = _fresh(1, members, meta, rng, (), now)
            changed = True
        else:
            members = _members_for(key, state, members)
            changed = _sync(state, members, rng)
        if state["order"] and not _remaining(state):
            state = _roll(state, members, meta, rng, now)
            changed = True
        remaining = _remaining(state)
        if len(remaining) < NEXT_WHEN_LEFT and state.get("next") is None and members:
            tail = (state["heard"] + remaining)[-junction_size(len(state["order"])) :]
            state["next"] = build(members, meta, rng, tail)
            changed = True
        if changed:
            _save(key, state)

        wanted = set(only) if only is not None else None
        busy = _leased_by_others(key, device, now)
        order = [p for p in remaining if p not in busy and (wanted is None or p in wanted)]
        if not order:
            # Всё несыгранное забронировано другим устройством: лучше повтор,
            # чем тишина.
            order = [p for p in remaining if wanted is None or p in wanted]
        if wanted is not None:
            # Перемешивание очереди (кнопка в плеере): играет вся очередь —
            # несыгранное в этом круге первым, сыгранное за ним, тоже в порядке
            # колоды. Иначе очередь из двадцати найденных, уже слышанных в
            # круге, сжалась бы до пустой.
            have = set(order)
            order += [p for p in state["order"] if p in wanted and p not in have]
        nxt: list[str] = []
        if len(remaining) < NEXT_WHEN_LEFT and state.get("next"):
            nxt = [p for p in state["next"] if wanted is None or p in wanted][:NEXT_SHOWN]
        if device:
            _LEASES.setdefault(key, {})[device] = (
                now + LEASE_SECONDS,
                frozenset(order[:LEASE_SIZE]),
            )
        return {
            "key": key,
            "round": state["round"],
            "order": order,
            "next": nxt,
            "left": len(remaining),
            "total": len(state["order"]),
        }


def reshuffle(
    key: str,
    members: list[str],
    meta: Meta,
    device: str,
    only: Iterable[str] | None = None,
    now: float | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """«Перемешать заново»: новый порядок несыгранной части круга. Сыгранное
    не возвращается до конца круга (решение владельца 19.11.2026)."""
    now = time.time() if now is None else now
    rng = rng or random.Random()
    members = list(dict.fromkeys(members))
    with _LOCK:
        state = _load(key)
        if state is not None:
            members = _members_for(key, state, members)
            _sync(state, members, rng)
            heard = set(state["heard"])
            done = [p for p in state["order"] if p in heard]
            state["order"] = done + build(_remaining(state), meta, rng)
            # Следующую колоду не трогаем: другое устройство, может быть, уже
            # играет её начало, и его прослушивания должны начать тот же круг.
            _save(key, state)
        _LEASES.get(key, {}).pop(device, None)
    return take(key, members, meta, device, only, now, rng)


def _mark(state: dict, path: str, skipped: bool, played_seconds: float, rng: random.Random) -> bool:
    if path in state["heard"] or path not in state["order"]:
        return False
    if skipped and played_seconds < SKIP_SECONDS and path not in state["moved"]:
        # (Сюда доходит только пропуск в этой же колоде — см. heard.)
        state["moved"].append(path)
        heard = set(state["heard"])
        rest = [p for p in state["order"] if p not in heard and p != path]
        mine = [p for p in state["order"] if p not in heard].index(path)
        half = len(rest) // 2
        if mine < half:
            state["order"].remove(path)
            k = rng.randint(half, len(rest))
            if k < len(rest):
                state["order"].insert(state["order"].index(rest[k]), path)
            else:
                state["order"].append(path)
        return True
    state["heard"].append(path)
    return True


def heard(
    path: str,
    *,
    started: float,
    played_seconds: float,
    skipped: bool,
    source_key: str | None = None,
    source_round: int | None = None,
    now: float | None = None,
    rng: random.Random | None = None,
) -> list[str]:
    """Засчитать прослушивание во всех колодах, где есть ``path``.

    ``source_key``/``source_round`` — из какой колоды и какого её круга трек
    взят (плеер шлёт их вместе с прослушиванием). Трек из следующего круга
    начинает этот круг; из прошлого — уже ничего не значит (пришёл без сети
    после смены круга). Без них решает время: прослушивание, которое
    кончилось раньше начала круга, — про прошлый круг. Возвращает ключи колод, которые поменялись.
    """
    now = time.time() if now is None else now
    rng = rng or random.Random()
    changed: list[str] = []
    with _LOCK:
        for row in db.db_query("SELECT key FROM decks"):
            key = row["key"]
            state = _load(key)
            if state is None:
                continue
            in_next = state.get("next") is not None and path in state["next"]
            if path not in state["order"] and not in_next:
                continue
            early_skip = skipped and played_seconds < SKIP_SECONDS
            if early_skip and source_key != key:
                # «Любое прослушивание — во всех колодах» (решение 19.11.2026), а
                # пропуск — не прослушивание: двигает трек только в своей колоде.
                continue
            rolled = False
            own_round = source_key == key and source_round is not None
            if own_round and source_round == state["round"] + 1 and in_next:
                state = _roll(state, None, None, rng, now)
                rolled = True
            elif not (own_round and source_round == state["round"]):
                # Без метки или с меткой другого круга решает время: кончилось до
                # начала круга — это про прошлый круг (пришло без сети). Кончилось
                # позже — услышано сейчас: второе устройство доигрывает свои
                # треки прошлого круга, а этот уже начался.
                if started + played_seconds < state["started_at"]:
                    continue
            if _mark(state, path, skipped, played_seconds, rng) or rolled:
                _save(key, state)
                changed.append(key)
    return changed


def forget_leases() -> None:
    """Для тестов: бронь живёт в памяти процесса."""
    with _LOCK:
        _LEASES.clear()


def rename_path(old: str, new: str) -> None:
    """Трек заменили файлом с другим путём (library.carry_history): место в
    колодах и «сыграно» переходят к новому — иначе он прозвучал бы ещё раз в
    том же круге."""
    with _LOCK:
        for row in db.db_query("SELECT key FROM decks"):
            state = _load(row["key"])
            if state is None:
                continue
            changed = False
            for field in ("order", "heard", "moved", "next"):
                items = state.get(field)
                if not items or old not in items:
                    continue
                state[field] = list(dict.fromkeys(new if p == old else p for p in items))
                changed = True
            if changed:
                _save(row["key"], state)


def prune(days: int) -> None:
    """Колоды, которых не касались ``days`` дней (подборку удалили или
    переименовали), — долой: heard перебирает все колоды на каждое прослушивание."""
    db.db_exec("DELETE FROM decks WHERE updated_at < ?", (time.time() - days * 86400,))
