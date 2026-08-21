"""Canonical production launcher for the local-Spotify adder service.

Keeps worker startup and recovery outside app.py's import path so the
systemd service and direct ASGI deployments use the same runtime setup.
"""
import threading

import uvicorn

from .app import (
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
    worker,
)


def main() -> None:
    PROJECT.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_init()
    cleanup_old_temp_files()
    recover_queued_tasks()

    for _ in range(MAX_WORKERS):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        active_workers.append(thread)

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
