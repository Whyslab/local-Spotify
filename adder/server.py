"""Canonical production launcher for the local-Spotify adder service.

Keeps worker startup and recovery outside app.py's import path so the
systemd service and direct ASGI deployments use the same runtime setup.
"""
import signal

import uvicorn

from app import (
    HOST,
    PORT,
    MAX_WORKERS,
    PROJECT,
    TMP_DIR,
    active_workers,
    app,
    cleanup_old_temp_files,
    db_init,
    recover_queued_tasks,
    shutdown_handler,
    worker,
)


def main() -> None:
    PROJECT.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_init()
    cleanup_old_temp_files()
    recover_queued_tasks()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    for _ in range(MAX_WORKERS):
        thread = __import__("threading").Thread(target=worker, daemon=True)
        thread.start()
        active_workers.append(thread)

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
