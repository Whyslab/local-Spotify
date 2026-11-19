"""Похожесть артистов фонотеки: ListenBrainz, Deezer и свои сессии.

Три источника, каждый закрывает дыру другого:

- **ListenBrainz similar-artists** — кого слушают в одних сессиях люди;
  для русского рэпа правдоподобно (PHARAOH → Boulevard Depo, Mnogoznaal,
  ЛСП…), но нишевых и новых артистов не знает. MBID артиста — через поиск
  записи MusicBrainz по треку из фонотеки: поиск по имени отдаёт тёзок
  (на «PHARAOH» первой нашлась метал-группа из Филадельфии).
- **Deezer related** — запасной для нишевых. id артиста — тоже через поиск
  трека: ``search/artist`` на «PHARAOH» ставит первым тёзку с 16 слушателями.
- **Свои сессии** — соседние треки, оба дослушанные: так слушает сам
  владелец, и это знание есть даже для тех, кого не знает никто.

Сеть — только в ночном проходе (``refresh``, scripts/build_artist_graph.py),
ответы лежат в ``adder/artist-graph.json``. Очередь читает файл и журнал,
никуда не ходит.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import runtime

logger = logging.getLogger(__name__)

GRAPH_FILE = runtime.PROJECT / "artist-graph.json"

# Без имени и почты владельца (частное): MusicBrainz пускает и так, если
# клиент назвался — анонимный запрос получает 403.
USER_AGENT = "local-music-server/1.0 ( personal use )"
MB_RECORDING = "https://musicbrainz.org/ws/2/recording/"
LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
LB_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
)
DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_RELATED = "https://api.deezer.com/artist/{id}/related"
# MusicBrainz — не чаще раза в секунду (их правило), с запасом.
MB_PACE = 1.3
OTHER_PACE = 0.3
TIMEOUT = 15
# Похожих пересчитывать раз в месяц; id артиста не меняется — навсегда.
REFRESH_AFTER = 30 * 24 * 3600
# Промах («не знает») — повторить через неделю: могли завести.
MISS_RETRY = 7 * 24 * 3600

# Свои сессии: пара артистов, дослушанных подряд, — столько раз до «похожи
# наполовину»: own = n / (n + OWN_HALF).
OWN_HALF = 3.0
SESSION_GAP = 30 * 60


def key(name: str) -> str:
    """Имя артиста для сравнения: регистр, «ё», знаки и пробелы не важны."""
    text = (name or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def parts(artist: str) -> list[str]:
    """Артисты строки тега: «A • B» и «A feat. B» — оба."""
    raw = re.split(r"•|\bfeat\.?\s|\bft\.?\s", artist or "", flags=re.IGNORECASE)
    return [k for k in (key(p) for p in raw) if k]


# ---------------------------------------------------------------------------
# Чтение


_LOADED: dict[str, Any] = {"mtime": None, "edges": {}}
_OWN: dict[str, Any] = {"at": 0.0, "edges": {}}
_LOCK = threading.Lock()
OWN_TTL = 600


def _graph_edges() -> dict[str, dict[str, float]]:
    try:
        mtime = GRAPH_FILE.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _LOADED["mtime"] == mtime:
            return _LOADED["edges"]
    try:
        data = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    edges = edges_from(data)
    with _LOCK:
        _LOADED.update(mtime=mtime, edges=edges)
    return edges


def edges_from(data: dict) -> dict[str, dict[str, float]]:
    """Похожесть 0..1 из ответов ListenBrainz и Deezer, в обе стороны.

    ListenBrainz — доля от лучшего счёта у этого артиста; Deezer — по месту
    в списке (первый 1,0, двадцатый 0,5). Из двух источников — больший.
    """
    edges: dict[str, dict[str, float]] = {}

    def put(a: str, b: str, score: float) -> None:
        if not a or not b or a == b:
            return
        for x, y in ((a, b), (b, a)):
            slot = edges.setdefault(x, {})
            slot[y] = max(slot.get(y, 0.0), score)

    for name, entry in (data.get("artists") or {}).items():
        lb = entry.get("lb") or []
        top = max((float(item.get("score") or 0) for item in lb), default=0.0)
        for item in lb:
            if top > 0:
                put(name, key(item.get("name", "")), float(item.get("score") or 0) / top)
        dz = entry.get("deezer") or []
        for rank, other in enumerate(dz):
            put(name, key(other), 1.0 - 0.5 * rank / max(len(dz) - 1, 1))
    return edges


def own_edges(plays: list[dict], artist_of: dict[str, str]) -> dict[str, dict[str, float]]:
    """Похожесть из своих сессий: соседние треки, оба дослушанные.

    ``plays`` — от старых к новым: path, at (секунды), skipped; ``ordered``
    (играли по порядку, альбом) — такие пары ничего не говорят о вкусе.
    """
    counts: dict[tuple[str, str], int] = {}
    previous: dict | None = None
    for play in plays:
        if previous is not None and play["at"] - previous["at"] > SESSION_GAP:
            previous = None
        if play.get("skipped") or play.get("ordered"):
            previous = None
            continue
        if previous is not None:
            for a in parts(artist_of.get(previous["path"], "")):
                for b in parts(artist_of.get(play["path"], "")):
                    if a != b:
                        pair = (a, b) if a < b else (b, a)
                        counts[pair] = counts.get(pair, 0) + 1
        previous = play
    edges: dict[str, dict[str, float]] = {}
    for (a, b), n in counts.items():
        score = n / (n + OWN_HALF)
        edges.setdefault(a, {})[b] = score
        edges.setdefault(b, {})[a] = score
    return edges


def _own() -> dict[str, dict[str, float]]:
    with _LOCK:
        if time.monotonic() - _OWN["at"] < OWN_TTL and _OWN["at"]:
            return _OWN["edges"]
    from . import db, library

    rows = db.db_query(
        "SELECT path, CAST(strftime('%s', played_at) AS REAL) AS at, skipped, "
        "(mode = 'manual' AND shuffled = 0) AS ordered "
        "FROM plays ORDER BY played_at, id"
    )
    artist_of = {row["path"]: row.get("artist") or "" for row in library.library_index()}
    edges = own_edges(rows, artist_of)
    with _LOCK:
        _OWN.update(at=time.monotonic(), edges=edges)
    return edges


def similarity_function():
    """Функция похожести двух списков артистов (ключи ``key``) для очереди.

    Тот же артист — 1; иначе лучшая пара из графа и своих сессий.
    """
    graph = _graph_edges()
    own = _own()

    def similar(first: list[str], second: list[str]) -> float:
        a = {p for x in first for p in parts(x)}
        b = {p for x in second for p in parts(x)}
        if a & b:
            return 1.0
        best = 0.0
        for x in a:
            for y in b:
                best = max(best, graph.get(x, {}).get(y, 0.0), own.get(x, {}).get(y, 0.0))
        return best

    return similar


def forget() -> None:
    with _LOCK:
        _LOADED.update(mtime=None, edges={})
        _OWN.update(at=0.0, edges={})


# ---------------------------------------------------------------------------
# Ночной проход (сеть)


class _Pacer:
    def __init__(self, pause: float) -> None:
        self.pause = pause
        self.last = 0.0

    def wait(self) -> None:
        delay = self.last + self.pause - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


# MusicBrainz отвечает 503, когда общий поток запросов к нему велик, — и при
# нашем темпе раз в 1,3 с (19.11: 38 отказов на 53 артистов). Повтор через
# паузу почти всегда проходит.
RETRY_PAUSES = [3.0, 8.0]


def _json(client: httpx.Client, pacer: _Pacer, url: str, params: dict) -> Any:
    """Ответ JSON или исключение: сбой не должен записаться как «не знает»."""
    for pause in (*RETRY_PAUSES, None):
        pacer.wait()
        response = client.get(url, params=params)
        if response.status_code not in (429, 503) or pause is None:
            break
        time.sleep(pause)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and "error" in body and "data" not in body:
        raise httpx.HTTPError(f"ошибка: {body['error']}")
    return body


def _quote(text: str) -> str:
    return '"' + text.replace("\\", " ").replace('"', " ") + '"'


def find_mbid(client: httpx.Client, pacer: _Pacer, name: str, titles: list[str]) -> str:
    """MBID артиста через запись MusicBrainz; пусто — не нашёлся."""
    for title in titles[:3]:
        body = _json(
            client,
            pacer,
            MB_RECORDING,
            {
                "query": f"recording:{_quote(title)} AND artist:{_quote(name)}",
                "fmt": "json",
                "limit": 5,
            },
        )
        for recording in (body or {}).get("recordings") or []:
            for credit in recording.get("artist-credit") or []:
                artist = credit.get("artist") if isinstance(credit, dict) else None
                if artist and key(artist.get("name", "")) == key(name):
                    return str(artist.get("id") or "")
    return ""


def find_deezer(client: httpx.Client, pacer: _Pacer, name: str, titles: list[str]) -> int:
    """id артиста Deezer через поиск трека; 0 — не нашёлся."""
    for title in titles[:3]:
        body = _json(client, pacer, DEEZER_SEARCH, {"q": f"{name} {title}", "limit": 10})
        for row in (body or {}).get("data") or []:
            artist = row.get("artist") or {}
            if key(artist.get("name", "")) == key(name):
                return int(artist.get("id") or 0)
    return 0


def _load_file() -> dict:
    try:
        return json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"artists": {}}


def _save_file(data: dict) -> None:
    tmp = GRAPH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, GRAPH_FILE)


def library_artists(index_rows: list[dict]) -> dict[str, dict]:
    """Ключ артиста → его имя как в тегах и несколько названий треков."""
    out: dict[str, dict] = {}
    for row in index_rows:
        raw = row.get("artist") or ""
        names = re.split(r"•|\bfeat\.?\s|\bft\.?\s", raw, flags=re.IGNORECASE)
        for name in names:
            name = " ".join(name.split())
            k = key(name)
            if not k:
                continue
            slot = out.setdefault(k, {"name": name, "titles": []})
            title = (row.get("title") or "").strip()
            if title and len(slot["titles"]) < 5:
                slot["titles"].append(title)
    return out


def refresh(
    index_rows: list[dict],
    budget: float = 1800.0,
    now: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Дополнить ``artist-graph.json``. Возвращает счётчики прохода.

    Продолжает с места остановки: что спрошено недавно — не спрашивается.
    Сохраняется после каждого артиста, так что обрыв ничего не теряет.
    """
    now = time.time() if now is None else now
    data = _load_file()
    artists = data.setdefault("artists", {})
    wanted = library_artists(index_rows)
    started = time.monotonic()
    asked = failed = 0
    mb, other = _Pacer(MB_PACE), _Pacer(OTHER_PACE)
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(
        timeout=TIMEOUT, headers=headers, follow_redirects=True, transport=transport
    ) as client:
        for k, info in sorted(wanted.items()):
            if time.monotonic() - started > budget:
                break
            entry = artists.setdefault(k, {})
            at = entry.get("at")
            if at is not None:
                age = now - float(at)
                if age < (MISS_RETRY if entry.get("missed") else REFRESH_AFTER):
                    continue
            try:
                if not entry.get("mbid"):
                    entry["mbid"] = find_mbid(client, mb, info["name"], info["titles"])
                if not entry.get("deezer_id"):
                    entry["deezer_id"] = find_deezer(client, other, info["name"], info["titles"])
                lb: list[dict] = []
                if entry["mbid"]:
                    body = _json(
                        client,
                        other,
                        LB_SIMILAR,
                        {"artist_mbids": entry["mbid"], "algorithm": LB_ALGORITHM},
                    )
                    lb = [
                        {"name": item.get("name", ""), "score": item.get("score", 0)}
                        for item in (body if isinstance(body, list) else [])
                        if isinstance(item, dict)
                    ][:50]
                dz: list[str] = []
                if entry["deezer_id"]:
                    body = _json(
                        client,
                        other,
                        DEEZER_RELATED.format(id=entry["deezer_id"]),
                        {"limit": 25},
                    )
                    dz = [row.get("name", "") for row in (body or {}).get("data") or []]
            except (httpx.HTTPError, ValueError) as exc:
                # Сбой — не «не знает»: ничего не записываем, спросим завтра.
                failed += 1
                logger.info("граф артистов: %s — %s", info["name"], exc)
                if failed >= 20 and failed > asked:
                    logger.warning("граф артистов: сеть отказывает, проход прерван")
                    break
                continue
            entry.update(lb=lb, deezer=dz, at=now, missed=not lb and not dz)
            asked += 1
            _save_file(data)
    data["built_at"] = now
    _save_file(data)
    return {"asked": asked, "failed": failed, "artists": len(wanted)}


def describe(path: Path = GRAPH_FILE) -> dict:
    data = _load_file() if path == GRAPH_FILE else json.loads(path.read_text(encoding="utf-8"))
    artists = data.get("artists") or {}
    return {
        "artists": len(artists),
        "with_listenbrainz": sum(1 for e in artists.values() if e.get("lb")),
        "with_deezer": sum(1 for e in artists.values() if e.get("deezer")),
        "unknown": sum(1 for e in artists.values() if e.get("missed")),
    }
