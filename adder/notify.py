"""Desktop notifications on the machine the service runs on.

"Track added" and "download failed" used to be visible only in the panel, so a
failure went unnoticed until someone looked. This pops up a notification the
way any desktop application does: notify-send over the session D-Bus.

Events are gathered for a few seconds before anything is shown. A playlist
import finishes fifty tasks in a row, and fifty pop-ups would be noise; one
"Добавлено 50 треков" is information.

Nothing here may break a download: a missing notify-send, no desktop session or
a hung notification daemon are all logged at debug level and ignored.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

APP_NAME = "local-Spotify"
ICON = "audio-x-generic"
# How long events are gathered before one notification is shown.
BATCH_SECONDS = 8.0
# How many names a batched notification lists before "и ещё N".
SHOW_NAMES = 3


def _environment() -> dict[str, str]:
    """The environment notify-send needs to find the desktop's session bus.

    A systemd user unit normally inherits DBUS_SESSION_BUS_ADDRESS from the
    user manager; when it does not, the bus is at its standard place.
    """
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(f"/run/user/{os.getuid()}/bus")
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _show(summary: str, body: str, urgent: bool) -> None:
    program = shutil.which("notify-send")
    if not program:
        logger.debug("notify-send not installed; notification skipped", extra={"task_id": "system"})
        return
    command = [
        program,
        "--app-name",
        APP_NAME,
        "--icon",
        ICON,
        "--urgency",
        "critical" if urgent else "normal",
        summary,
        body,
    ]
    try:
        subprocess.run(command, env=_environment(), timeout=5, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Notification failed: %s", exc, extra={"task_id": "system"})


def send(summary: str, body: str = "", urgent: bool = False) -> None:
    """Show one notification now, in the background."""
    if not config.DESKTOP_NOTIFICATIONS:
        return
    threading.Thread(target=_show, args=(summary, body, urgent), name="notify", daemon=True).start()


class _Batch:
    """Events of one kind, gathered and shown as a single notification."""

    def __init__(self, one: str, many: str, urgent: bool) -> None:
        self.one, self.many, self.urgent = one, many, urgent
        self.items: list[str] = []
        self.timer: threading.Timer | None = None
        self.lock = threading.Lock()

    def add(self, item: str) -> None:
        if not config.DESKTOP_NOTIFICATIONS:
            return
        with self.lock:
            self.items.append(item)
            if self.timer is None:
                self.timer = threading.Timer(BATCH_SECONDS, self.flush)
                self.timer.daemon = True
                self.timer.start()

    def message(self, items: list[str]) -> tuple[str, str]:
        if len(items) == 1:
            return self.one, items[0]
        shown = items[:SHOW_NAMES]
        rest = len(items) - len(shown)
        body = "\n".join(shown) + (f"\nи ещё {rest}" if rest else "")
        return self.many.format(count=len(items)), body

    def flush(self) -> None:
        with self.lock:
            items, self.items = self.items, []
            self.timer = None
        if items:
            summary, body = self.message(items)
            _show(summary, body, self.urgent)


_added = _Batch("Трек добавлен", "Добавлено треков: {count}", urgent=False)
_failed = _Batch("Не удалось скачать", "Не удалось скачать: {count}", urgent=True)


def track_added(artist: str, title: str) -> None:
    _added.add(f"{artist} — {title}" if artist else title)


def track_failed(name: str, reason: str) -> None:
    _failed.add(f"{name}: {reason}")


def flush_now() -> None:
    """Show whatever is gathered without waiting (used at shutdown and in tests)."""
    for batch in (_added, _failed):
        with batch.lock:
            timer = batch.timer
        if timer:
            timer.cancel()
        batch.flush()
