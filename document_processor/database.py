"""Open the SQLite database and upgrade its schema with numbered migrations.

Each migration upgrades the schema by one version, runs in its own
transaction and is recorded in the schema_migrations table, so it runs once
per database. To change the schema, append a new function to MIGRATIONS and
never edit a released one. Migrations only add tables, columns and indexes:
existing rows are never rewritten.

Databases created before version tracking may already contain part of the
schema, so every migration must also be safe to run on such a database
(CREATE ... IF NOT EXISTS, add a column only when it is missing).

Startup uses short-lived connections (connect). The running application keeps
one connection per thread (ThreadConnections).
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .observability import event


class SchemaVersionError(RuntimeError):
    """The database was upgraded by a newer version of the application."""


class _Connection(sqlite3.Connection):
    """A connection that closes itself when it is dropped.

    A thread's connection lives as long as the thread, and the threads of the
    web server cannot close theirs. Python 3.13 and later warn about a
    connection that was never closed explicitly.
    """

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass  # dropped by another thread than the one that opened it: SQLite still frees it


def _open(path):
    """Open a connection: dictionary-like rows (sqlite3.Row), foreign keys, a 20 second wait for locks.

    In WAL mode (see initialize) synchronous=NORMAL lets a commit return
    without waiting for the disk. A power failure can then undo the latest
    commits but never corrupts the database. Without WAL, the database keeps
    SQLite's default, which is the only safe setting there.
    """
    con = sqlite3.connect(path, timeout=20, factory=_Connection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    if con.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        con.execute("PRAGMA synchronous=NORMAL")
    return con


@contextmanager
def connect(path):
    """Yield a short-lived connection whose transaction commits on success, rolls back on error.

    A locked database is retried for up to 20 seconds, and the connection is
    always closed afterward.
    """
    con = _open(path)
    try:
        with con:
            yield con
    finally:
        con.close()


class ThreadConnections:
    """Give each thread its own connection to the database and keep it open between uses.

    Opening a connection for every query cost far more than the query: it
    created and deleted the -wal and -shm files, because closing the last
    connection of a WAL database folds the log into it, and every commit
    waited for the disk. The application has few threads (the web threads and
    the worker), so each one opens its connection on first use. A connection
    never leaves its thread, so no lock is needed.
    """

    def __init__(self, path):
        self.path = path
        self._local = threading.local()

    @contextmanager
    def transaction(self):
        """Yield this thread's connection; commit on success, roll back on error.

        It works like connect(), except that the connection stays open. Blocks
        cannot be nested: the inner one would commit the work of the outer
        one. After a SQLite error the connection is closed and replaced at the
        next use, as every use of connect() starts from a fresh one.
        """
        local = self._local
        if getattr(local, "busy", False):
            raise RuntimeError("Database transactions cannot be nested.")
        con = getattr(local, "connection", None)
        if con is None:
            con = local.connection = _open(self.path)
        local.busy = True
        try:
            with con:
                yield con
        except sqlite3.Error:
            local.connection = None
            con.close()
            raise
        finally:
            local.busy = False

    def close(self):
        """Fold the write-ahead log into the database file, then close this thread's connection.

        Call it when the process stops, so that the database file alone holds
        every committed change. The connection of another thread may stay
        open: an idle one does not prevent the folding. If it fails, the next
        start recovers the log anyway.
        """
        con, self._local.connection = getattr(self._local, "connection", None), None
        if con is None:
            return
        try:
            # A thread that is writing only delays the folding: do not hold up the shutdown.
            con.execute("PRAGMA busy_timeout=1000")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            event("sqlite_checkpoint_failed", logging.WARNING, error_type=type(exc).__name__)
        finally:
            con.close()


def _add_columns(con, table, columns):
    """Add each missing column; columns maps names to SQL declarations."""
    existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            event("sqlite_migration", column=name, status="applied")


def _create_jobs(con):
    """Version 1: the jobs table, with the progress column added later to the first release."""
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
      status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
      pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
      error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      progress_percent INTEGER NOT NULL DEFAULT 0
    )""")
    _add_columns(con, "jobs", {"progress_percent": "INTEGER NOT NULL DEFAULT 0"})


def _add_configurations_and_measurements(con):
    """Version 2: phases, cancellation, configuration snapshots, attempts and steps."""
    _add_columns(con, "jobs", {
        "phase_code": "TEXT NOT NULL DEFAULT ''", "phase_args": "TEXT NOT NULL DEFAULT '{}'",
        "error_code": "TEXT NOT NULL DEFAULT ''", "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "queued_at": "TEXT",
    })
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


def _add_report_names_and_prompt_origin(con):
    """Version 3: descriptive report names and whether the request was customized."""
    _add_columns(con, "jobs", {"report_name": "TEXT", "question_custom": "INTEGER"})


# Ordered (version, migration) pairs. Append only; never edit a released migration.
MIGRATIONS = (
    (1, _create_jobs),
    (2, _add_configurations_and_measurements),
    (3, _add_report_names_and_prompt_origin),
)


def initialize(path):
    """Apply, in order, every migration this database has not recorded yet.

    Raise SchemaVersionError when the database was upgraded by a newer
    version of the application, instead of running with an unknown schema.
    """
    with connect(path) as con:
        event("sqlite_initializing", status="connected")
        # WAL lets the web threads read while the worker writes.
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            # Some file systems cannot share the memory WAL needs; see _open for what changes.
            event("sqlite_wal_unavailable", logging.WARNING, journal_mode=mode)
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {row["version"] for row in con.execute("SELECT version FROM schema_migrations")}
    latest = MIGRATIONS[-1][0]
    if applied and max(applied) > latest:
        raise SchemaVersionError(
            f"The database uses schema version {max(applied)}, newer than this application "
            f"supports ({latest}). Update the application or restore a matching backup.")
    for version, migrate in MIGRATIONS:
        if version in applied:
            continue
        with connect(path) as con:
            # One transaction per migration: a failure leaves the previous version intact.
            con.execute("BEGIN IMMEDIATE")
            migrate(con)
            con.execute("INSERT INTO schema_migrations VALUES (?, ?)",
                        (version, datetime.now(timezone.utc).isoformat(timespec="milliseconds")))
        event("schema_migrated", version=version, status="applied")
