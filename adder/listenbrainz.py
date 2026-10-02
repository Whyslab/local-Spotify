"""Sending what was listened to in the web player to ListenBrainz.

The play journal already records every finished or abandoned track. Those that
count as a listen by ListenBrainz's own rule - at least half the track or four
minutes, of a track at least 30 seconds long - are put in a small queue in the
database and sent in batches by a background thread. The queue is what makes
this safe to leave running: no network, a ListenBrainz outage or a restart only
delay the listens, never lose them.

Turned on by LISTENBRAINZ_TOKEN in adder/.env (the user token from
https://listenbrainz.org/settings/). A token ListenBrainz rejects stops sending
until the service restarts and says so, rather than retrying a bad token
forever.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from . import config, db, library, notify, outside, runtime

logger = logging.getLogger(__name__)

SUBMIT_URL = "https://api.listenbrainz.org/1/submit-listens"
BATCH = 100
SEND_EVERY_SECONDS = 60
MIN_TRACK_SECONDS = 30
ENOUGH_SECONDS = 240

_rejected = False


def enabled() -> bool:
    return bool(config.LISTENBRAINZ_TOKEN)


def counts_as_listen(played_seconds: float | None, duration: float | None) -> bool:
    """ListenBrainz's rule: half the track or four minutes, of a track >= 30 s."""
    if not played_seconds or not duration or duration < MIN_TRACK_SECONDS:
        return False
    return played_seconds >= min(ENOUGH_SECONDS, duration / 2)


def _describe(path: str) -> dict | None:
    if outside.is_outside(path):
        key = outside.track_key(path)
        meta = outside.read_meta(key) if key else None
        if not meta:
            return None
        return {
            "artist": meta.get("artist") or "",
            "title": meta.get("title") or "",
            "album": meta.get("album") or "",
            "source": "",
        }
    row = next((r for r in library.library_index() if r["path"] == path), None)
    if row is None:
        return None
    return {
        "artist": ", ".join(a for a in (row.get("artist") or "").split(" • ") if a),
        "title": row.get("title") or "",
        "album": row.get("album") or "",
        "source": row.get("source") or "",
    }


def queue_listen(
    path: str,
    played_seconds: float | None,
    duration: float | None,
    listened_at: float | None = None,
) -> bool:
    """Queue one play for sending if it counts as a listen. Returns whether it was queued.

    ``listened_at`` is when listening started (epoch seconds); without it, the
    play is taken to have just ended.
    """
    if not enabled() or not counts_as_listen(played_seconds, duration):
        return False
    meta = _describe(path)
    if not meta or not meta["artist"] or not meta["title"]:
        return False
    db.db_exec(
        "INSERT INTO listens(listened_at, artist, title, album, duration, origin_url) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (
            # When the listen started, which is what ListenBrainz means.
            int(listened_at if listened_at is not None else time.time() - (played_seconds or 0)),
            meta["artist"],
            meta["title"],
            meta["album"],
            duration,
            meta["source"],
        ),
    )
    return True


def _payload(row: dict) -> dict:
    info: dict[str, str | int] = {
        "submission_client": "local-Spotify",
        "media_player": "local-Spotify web player",
    }
    if row.get("duration"):
        info["duration_ms"] = int(row["duration"] * 1000)
    if row.get("origin_url"):
        info["origin_url"] = row["origin_url"]
    metadata = {"artist_name": row["artist"], "track_name": row["title"], "additional_info": info}
    if row.get("album"):
        metadata["release_name"] = row["album"]
    return {"listened_at": row["listened_at"], "track_metadata": metadata}


def send_pending() -> int:
    """Send one batch of queued listens. Returns how many were accepted."""
    if not enabled() or _rejected:
        return 0
    rows = db.db_query(
        "SELECT * FROM listens WHERE sent = 0 ORDER BY listened_at LIMIT ?", (BATCH,)
    )
    if not rows:
        return 0
    _refused_this_pass[0] = 0
    return _send(rows)


# sent = -1: ListenBrainz refused this listen itself (HTTP 400). Not sent and
# never retried -- it would be refused again and hold up everything after it.
REFUSED = -1
# Не больше стольких отказов за проход. Если 400 приходит на каждую строку,
# дело не в строках (наш запрос сломан, сервис поменялся) — и делёж пачки
# пополам пометил бы отвергнутой всю очередь навсегда. Сверх предела строки
# остаются в очереди до следующего прохода.
MAX_REFUSED_PER_PASS = 5
_refused_this_pass = [0]


def _send(rows: list[dict]) -> int:
    global _rejected
    body = {
        "listen_type": "import" if len(rows) > 1 else "single",
        "payload": [_payload(r) for r in rows],
    }
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    try:
        response = requests.post(
            SUBMIT_URL,
            json=body,
            headers={"Authorization": f"Token {config.LISTENBRAINZ_TOKEN}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.info("ListenBrainz unreachable, will retry: %s", exc, extra={"task_id": "system"})
        db.db_exec(f"UPDATE listens SET attempts = attempts + 1 WHERE id IN ({marks})", ids)
        return 0

    if response.status_code == 200:
        db.db_exec(f"UPDATE listens SET sent = 1 WHERE id IN ({marks})", ids)
        logger.info("Sent %d listen(s) to ListenBrainz", len(ids), extra={"task_id": "system"})
        return len(ids)
    if response.status_code == 401:
        _rejected = True
        logger.error(
            "ListenBrainz rejected LISTENBRAINZ_TOKEN; not sending until the service restarts",
            extra={"task_id": "system"},
        )
        notify.send(
            "ListenBrainz не принял токен", "Проверьте LISTENBRAINZ_TOKEN в adder/.env", urgent=True
        )
        return 0
    if response.status_code == 400:
        # Пачка с одной негодной строкой отвергается целиком, и повтор той же
        # пачки держал бы очередь вечно. Делим пополам, пока не останется
        # сама негодная строка, — остальные уходят.
        if len(rows) > 1:
            half = len(rows) // 2
            accepted = _send(rows[:half])
            return accepted + (0 if _rejected else _send(rows[half:]))
        if _refused_this_pass[0] >= MAX_REFUSED_PER_PASS:
            logger.warning(
                "ListenBrainz refuses listen after listen; keeping the rest queued",
                extra={"task_id": "system"},
            )
            return 0
        _refused_this_pass[0] += 1
        db.db_exec("UPDATE listens SET sent = ? WHERE id = ?", (REFUSED, ids[0]))
        logger.warning(
            "ListenBrainz refused a listen of %r by %r, dropped: %s",
            rows[0]["title"],
            rows[0]["artist"],
            response.text[:200],
            extra={"task_id": "system"},
        )
        return 0
    logger.warning(
        "ListenBrainz answered %s, will retry: %s",
        response.status_code,
        response.text[:200],
        extra={"task_id": "system"},
    )
    db.db_exec(f"UPDATE listens SET attempts = attempts + 1 WHERE id IN ({marks})", ids)
    return 0


def pending_count() -> int:
    rows = db.db_query("SELECT COUNT(*) AS n FROM listens WHERE sent = 0")
    return rows[0]["n"] if rows else 0


def status() -> str:
    if not enabled():
        return "off"
    if _rejected:
        return "token rejected"
    pending = pending_count()
    return f"{pending} waiting" if pending else "ok"


def _loop() -> None:
    while not runtime.shutdown_event.wait(SEND_EVERY_SECONDS):
        try:
            while send_pending() == BATCH:
                pass  # a backlog goes out in consecutive batches
        except Exception:
            logger.exception("ListenBrainz sending failed", extra={"task_id": "system"})


def start() -> None:
    if enabled():
        threading.Thread(target=_loop, name="listenbrainz", daemon=True).start()
