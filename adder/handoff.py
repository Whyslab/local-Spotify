"""«Продолжить на другом устройстве»: что и с какого места играло последним.

Слушал на ноутбуке, взял телефон — там плашка «Играло на ноутбуке: трек, 1:23
· Продолжить», и нажатие включает тот же трек с того же места и с той же
очередью. Как в Spotify, только без управления чужим устройством на расстоянии:
плеер — страница в браузере, и сервер не может ей ничего послать, пока она
сама не спросит.

Что где:

* Каждое устройство (браузер со своим ``device`` в localStorage) раз в
  несколько секунд шлёт ``report``: трек, место, играет ли. Очередь целиком —
  только когда она поменялась (её размер — до всей фонотеки).
* Хранится одна запись — последнего, кто сообщал. В базе (таблица
  ``settings``), чтобы пережить перезапуск службы.
* «Чьё сейчас» (``owner``) — устройство, на котором последний раз нажали
  «играть» (``claim``). Если другое устройство всё ещё играет и шлёт
  отчёт, ответ ему — ``yield``: встань на паузу, слушают уже там. Так
  «Продолжить» на телефоне останавливает ноутбук в пределах одного отчёта.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import db

logger = logging.getLogger(__name__)

STATE_KEY = "handoff_now"
QUEUE_KEY = "handoff_queue"

_LOCK = threading.Lock()


def _load(key: str) -> dict | None:
    try:
        rows = db.db_query("SELECT value FROM settings WHERE key = ?", (key,))
    except Exception:  # noqa: BLE001 — нет базы: продолжать нечего
        return None
    if not rows:
        return None
    try:
        value = json.loads(rows[0]["value"])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _store(key: str, value: dict) -> None:
    db.db_exec(
        "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def report(
    device: str,
    label: str,
    track: dict | None,
    index: int,
    at: float,
    playing: bool,
    claim: bool,
    stamp: int,
    queue: dict | None = None,
) -> dict:
    """Записать, что играет на ``device``. Ответ: ``{"yield": bool, "to": label}``.

    ``stamp`` — метка очереди на устройстве (меняется, когда очередь правят);
    ``queue`` — она целиком, когда поменялась. Место без очереди принимается,
    только если метка та же, что у сохранённой очереди: иначе «продолжить»
    включило бы чужой трек в старой очереди.
    """
    now = time.time()
    with _LOCK:
        state = _load(STATE_KEY) or {}
        owner = state.get("owner")
        if claim or not owner:
            owner = device
            # Сразу в базу: даже если ниже окажется, что очереди нет
            # (need_queue), «слушают здесь» не должно потеряться — иначе
            # следующий отчёт этого же устройства получил бы «пауза».
            if state.get("owner") != device:
                state["owner"] = device
                state["owner_label"] = label
                _store(STATE_KEY, state)
        if owner != device and playing:
            # Слушают уже на другом устройстве — этому встать на паузу.
            return {"yield": True, "to": state.get("owner_label") or ""}
        if owner != device:
            # На паузе и не владелец — ничего не меняем: запись про то, что
            # играет там, где слушают.
            return {"yield": False, "to": state.get("owner_label") or ""}
        if queue is not None:
            _store(QUEUE_KEY, {"stamp": stamp, "device": device, **queue})
        else:
            saved = _load(QUEUE_KEY)
            if not saved or saved.get("stamp") != stamp or saved.get("device") != device:
                # Очереди к этой метке нет — место без неё не сохранить; пусть
                # устройство пришлёт очередь (need_queue).
                return {"yield": False, "to": "", "need_queue": True}
        _store(
            STATE_KEY,
            {
                "device": device,
                "label": label,
                "owner": owner,
                "owner_label": label,
                "track": track,
                "index": index,
                "at": at,
                "playing": playing,
                "stamp": stamp,
                "updated": now,
            },
        )
    return {"yield": False, "to": ""}


# Отчёты идут раз в 10 с (в фоне айфона — от timeupdate); молчит дольше
# минуты — устройство уже не играет (закрыли вкладку посреди трека, уснул
# телефон), и место дальше не досчитывать. Запас — на засыпающие в фоне таймеры.
SILENT_AFTER = 60


def current() -> dict | None:
    """Последнее, что играло: для плашки на другом устройстве.

    ``age`` — сколько секунд назад пришёл отчёт, по часам сервера: часы
    телефона и ноутбука могут расходиться. ``at`` — место сейчас: у ещё
    играющего досчитано на ``age``, но не дальше конца трека.
    """
    state = _load(STATE_KEY)
    if not state or not state.get("track"):
        return None
    out = {
        key: state.get(key)
        for key in ("device", "label", "track", "index", "at", "playing", "stamp", "updated")
    }
    age = max(0.0, time.time() - float(state.get("updated") or 0))
    out["age"] = round(age, 1)
    if out["playing"] and age > SILENT_AFTER:
        out["playing"] = False
    if out["playing"]:
        at = float(out["at"] or 0) + age
        duration = (state.get("track") or {}).get("duration")
        if isinstance(duration, int | float) and duration > 0:
            at = min(at, max(0.0, float(duration) - 1))
        out["at"] = round(at, 1)
    return out


def saved_queue(stamp: int) -> dict[str, Any] | None:
    """Очередь к метке ``stamp`` — чтобы продолжить ровно с неё."""
    saved = _load(QUEUE_KEY)
    if not saved or saved.get("stamp") != stamp:
        return None
    return saved


def take(device: str, label: str) -> None:
    """Нажали «Продолжить» на ``device``: теперь слушают здесь."""
    with _LOCK:
        state = _load(STATE_KEY) or {}
        state["owner"] = device
        state["owner_label"] = label
        _store(STATE_KEY, state)
