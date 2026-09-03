"""The background queue: retry policy, worker threads, recovery after a restart.

This module owns *when* a track is processed and what happens when that fails.
What actually happens to the bytes lives in :mod:`adder.ingest`.
"""

import logging
import queue as _queue
from pathlib import Path

from . import config, db, ingest, runtime

logger = logging.getLogger(__name__)


def recover_queued_tasks():
    """Requeue tasks that were in flight when the service last stopped."""
    queued = db.db_query(
        "SELECT id, url, status FROM tasks WHERE status IN ('queued', 'downloading', 'tagging')"
    )
    recovered = 0
    for task in queued:
        # Reset interrupted tasks to queued
        if task["status"] in ("downloading", "tagging"):
            db.task_update(task["id"], status="queued")
        # Add to queue (avoiding duplicates)
        with runtime.FILE_LOCK:
            if task["url"] not in runtime.PROCESSING_URLS:
                runtime.TASK_QUEUE.put((task["id"], task["url"]))
                runtime.PROCESSING_URLS.add(task["url"])
                recovered += 1
    if recovered > 0:
        logger.info(
            f"Recovered {recovered} tasks from previous session", extra={"task_id": "system"}
        )


def _discard_temp(temp_path: Path | None, tid: int) -> None:
    if temp_path and temp_path.exists():
        try:
            temp_path.unlink()
        except OSError as cleanup_error:
            logger.warning(
                f"Could not remove temporary file {temp_path}: {cleanup_error}",
                extra={"task_id": tid},
            )


def process(tid: int, url: str):
    temp_path = None
    retry_count = 0
    last_error_type = None

    while retry_count == 0 or retry_count < config.MAX_RETRIES:
        try:
            downloaded = ingest.download_to_temp(tid, url)
            temp_path = downloaded.temp_path

            outcome = ingest.ingest_temp_file(
                tid, temp_path, downloaded.names, downloaded.thumbnail
            )
            temp_path = None  # consumed by the ingest half, nothing left to clean up

            db.task_update(tid, status="done", error="", error_type="")
            logger.info("Task finished: %s", outcome, extra={"task_id": tid})
            return  # Success, exit retry loop

        except runtime.ShutdownRequested:
            logger.info(
                "Task interrupted by shutdown; returning task to queued state",
                extra={"task_id": tid},
            )

            db.task_update(
                tid,
                status="queued",
                error="",
                error_type="",
                retry_count=retry_count,
            )

            _discard_temp(temp_path, tid)

            with runtime.FILE_LOCK:
                runtime.PROCESSING_URLS.discard(url)

            return

        except Exception as e:
            error_str = str(e)[:300]

            last_error_type = ingest.classify_error(error_str)

            if last_error_type in ingest.RETRYABLE_ERRORS and retry_count < config.MAX_RETRIES - 1:
                retry_count += 1
                backoff_time = config.RETRY_BACKOFF_BASE**retry_count
                logger.warning(
                    f"Retry {retry_count}/{config.MAX_RETRIES} after {backoff_time}s: {error_str}",
                    extra={"task_id": tid},
                )

                # Allow SIGTERM/shutdown to interrupt retry backoff immediately.
                if runtime.shutdown_event.wait(backoff_time):
                    logger.info(
                        "Shutdown requested during retry backoff",
                        extra={"task_id": tid},
                    )

                    # The normal cleanup below the retry loop is skipped by
                    # this early return, so release the processing lock here.
                    with runtime.FILE_LOCK:
                        runtime.PROCESSING_URLS.discard(url)

                    return

                continue

            # Not retryable or max retries reached
            db.task_update(
                tid,
                status="error",
                error=error_str,
                error_type=last_error_type,
                retry_count=retry_count,
            )

            _discard_temp(temp_path, tid)

            break  # Exit retry loop

    # Remove URL from processing set only after the entire task
    # (including all retry attempts) has finished.
    with runtime.FILE_LOCK:
        runtime.PROCESSING_URLS.discard(url)


def worker():
    while not runtime.shutdown_event.is_set():
        try:
            tid, url = runtime.TASK_QUEUE.get(timeout=1)
        except _queue.Empty:
            continue

        try:
            if runtime.shutdown_event.is_set():
                db.task_update(
                    tid,
                    status="queued",
                    error="",
                    error_type="",
                )

                with runtime.FILE_LOCK:
                    runtime.PROCESSING_URLS.discard(url)

                return

            process(tid, url)
        finally:
            runtime.TASK_QUEUE.task_done()
