"""SQLite access for the task table.

Connections are opened per call rather than pooled: the workload is a handful
of short statements per track, and a shared connection would have to be guarded
across threads anyway.
"""

import sqlite3
from contextlib import suppress

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
    # Small key/value store for things that must outlive a restart but do not
    # deserve a table of their own -- e.g. "the reconciliation sweep has run
    # once, it may now delete rather than only report".
    db_exec("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")

    # Work owed to Navidrome. A cover upload or a playlist deletion cannot be
    # made atomic with the file operation it accompanies, and Navidrome may be
    # down, so failures land here and are retried instead of being lost.
    db_exec("""CREATE TABLE IF NOT EXISTS navidrome_ops(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op TEXT NOT NULL, name TEXT NOT NULL, payload TEXT,
        created_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT)""")

    # The play journal. Navidrome keeps a play count and the date of the last
    # play, not a log, so "what was playing on Wednesday evenings" cannot be
    # asked of it at all. Anything that wants to know has to record its own.
    db_exec("""CREATE TABLE IF NOT EXISTS plays(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        played_at TEXT NOT NULL,
        played_seconds REAL,
        duration REAL,
        skipped INTEGER DEFAULT 0,
        source TEXT,
        mode TEXT DEFAULT 'manual')""")
    # Older databases predate the column; adding it here keeps an upgrade from
    # needing a migration step anyone has to remember to run.
    with suppress(sqlite3.OperationalError):
        db_exec("ALTER TABLE plays ADD COLUMN mode TEXT DEFAULT 'manual'")
    db_exec("CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays(played_at)")

    # Measured by scripts/analyze_audio.py. Kept in the same database as the
    # journal so a queue can be built from one connection.
    db_exec("""CREATE TABLE IF NOT EXISTS audio_features(
        path TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        tempo REAL, energy REAL, brightness REAL,
        music_key TEXT, mode TEXT,
        analyzed_at TEXT DEFAULT (datetime('now','localtime')))""")

    # Blind comparisons: two queues, one of each kind, and which one was
    # preferred without knowing which was which.
    db_exec("""CREATE TABLE IF NOT EXISTS blind_trials(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        smart_side TEXT NOT NULL,
        choice TEXT,
        decided_at TEXT)""")

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


def prune_play_history(days: int) -> int:
    """Drop journal entries older than ``days``. Returns how many went."""
    cur = db_exec(
        "DELETE FROM plays WHERE played_at < datetime('now', 'localtime', ?)",
        (f"-{int(days)} days",),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
