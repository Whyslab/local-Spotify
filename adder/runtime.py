"""Process-wide state shared by every module: paths, logging, the task queue.

Paths live here as module attributes rather than as values imported into each
consumer. Every consumer reads ``runtime.DB_PATH`` at call time, so a test that
points them at a temporary directory redirects the whole process. Importing the
value instead would leave each module holding its own private copy of the real
path, and the redirect would silently fail to apply.
"""

import logging
import os
import queue
import threading
from pathlib import Path

from .config import TMP_TTL_HOURS

PROJECT = Path(__file__).resolve().parent
TMP_DIR = PROJECT / "tmp"
DB_PATH = PROJECT / "adder.db"
# Deleted tracks land here rather than being unlinked, so a mistaken tap on a
# phone stays recoverable. It sits at the repository root, not under adder/,
# which is why the systemd unit needs its own ReadWritePaths entry for it.
TRASH_DIR = PROJECT.parent / "trash"

TMP_TTL_SECONDS = TMP_TTL_HOURS * 3600

TASK_QUEUE: queue.Queue = queue.Queue()
# Guards the duplicate check and the final move together, so two workers cannot
# both accept identical audio.
FILE_LOCK = threading.Lock()
# URLs currently being processed, to keep a resubmission from queueing twice.
PROCESSING_URLS: set = set()

shutdown_event = threading.Event()
active_workers: list = []


class ShutdownRequested(Exception):
    """Raised when a task is interrupted because the service is shutting down."""


class TaskIdFormatter(logging.Formatter):
    """Formatter that tolerates log records without task_id."""

    def format(self, record):
        if not hasattr(record, "task_id"):
            record.task_id = "-"
        return super().format(record)


_LOG_FORMAT = "[%(asctime)s] [task=%(task_id)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Install the tolerant formatter on the root handlers.

    Third-party libraries (httpx, uvicorn) log without a task_id; without this
    the format string above would raise KeyError on their records.
    """
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(TaskIdFormatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))


configure_logging()


def under_test() -> bool:
    """True while a pytest run is in progress."""
    return "PYTEST_CURRENT_TEST" in os.environ


def guard_real_library(library: Path, operation: str) -> None:
    """Refuse destructive work against a library that looks like the real one.

    The test fixtures redirect the library to a temporary directory. If a
    refactor ever breaks that redirect, the tests would keep passing while
    quietly moving the user's actual music into trash/. Failing loudly here is
    the difference between a red test run and a silent data loss.
    """
    if not under_test():
        return
    default = Path.home() / "Music" / "Normalized Library"
    if library.resolve() == default.resolve():
        raise RuntimeError(
            f"Refusing to {operation} against the real library at {library} during a test run. "
            "A fixture is no longer redirecting adder.config.LIBRARY."
        )
