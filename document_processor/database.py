"""Open the SQLite database and keep its schema up to date.

Only jobs.JobStore uses this module. The schema is created on first start and
upgraded in place on later starts; existing rows are never rewritten.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .observability import event


@contextmanager
def connect(path):
    """Yield a connection whose transaction commits on success, rolls back on error.

    Rows behave like dictionaries (sqlite3.Row). A locked database is retried
    for up to 20 seconds, and the connection is always closed afterward.
    """
    con = sqlite3.connect(path, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize(path):
    """Create missing tables and columns; safe to run on every start."""
    with connect(path) as con:
        event("sqlite_initializing", status="connected")
        # WAL lets the web threads read while the worker writes.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
          pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
          error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          progress_percent INTEGER NOT NULL DEFAULT 0
        )""")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(jobs)")}
        for name, declaration in {
            "progress_percent": "INTEGER NOT NULL DEFAULT 0",
            "phase_code": "TEXT NOT NULL DEFAULT ''", "phase_args": "TEXT NOT NULL DEFAULT '{}'",
            "error_code": "TEXT NOT NULL DEFAULT ''", "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "queued_at": "TEXT",
            "report_name": "TEXT", "question_custom": "INTEGER",
        }.items():
            if name not in columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
                event("sqlite_migration", column=name, status="applied")
        con.execute("""CREATE TABLE IF NOT EXISTS job_configs (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            duration_ms REAL, queue_ms REAL, provenance TEXT NOT NULL DEFAULT '{}')""")
        con.execute("""CREATE TABLE IF NOT EXISTS job_steps (
            id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL, stage TEXT NOT NULL, step_key TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            metrics TEXT NOT NULL DEFAULT '{}')""")
        con.execute("CREATE INDEX IF NOT EXISTS job_runs_by_job ON job_runs(job_id, id)")
        con.execute("CREATE INDEX IF NOT EXISTS job_steps_by_job ON job_steps(job_id, id)")
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (2, ?)", (applied_at,))
        con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (3, ?)", (applied_at,))
