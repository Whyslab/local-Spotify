"""SQLite access for the task table.

Connections are opened per call rather than pooled: the workload is a handful
of short statements per track, and a shared connection would have to be guarded
across threads anyway.
"""

import sqlite3

from . import runtime

logger = __import__("logging").getLogger(__name__)


def connect() -> sqlite3.Connection:
    """Open a connection with the journal mode this service needs.

    WAL matters here because three writers now share the file: the API, the
    play journal, and the audio analysis queue. Under the default rollback
    journal a long analysis transaction blocks readers outright, and the panel
    starts answering 500 while it runs.
    """
    con = sqlite3.connect(runtime.DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def db_exec(sql: str, params=()):
    con = connect()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur
    finally:
        con.close()


def db_query(sql: str, params=()):
    con = connect()
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def db_init():
    db_exec("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, status TEXT, artist TEXT, title TEXT, error TEXT,
        error_type TEXT, retry_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now','localtime')))""")
    # The unique index is what makes a resubmitted link idempotent. Local file
    # imports have no URL, so they are keyed by a synthetic "file:<sha256>"
    # source key that fits the same column and the same uniqueness rule.
    try:
        db_exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_url ON tasks(url)")
    except sqlite3.Error as e:
        logger.warning(
            f"Could not create task URL unique index: {e}",
            extra={"task_id": "system"},
        )


def task_update(tid: int, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    db_exec(
        f"UPDATE tasks SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
        (*fields.values(), tid),
    )
