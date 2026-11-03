"""Open the SQLite database and upgrade its schema with numbered migrations.

Each migration upgrades the schema by one version, runs in its own
transaction and is recorded in the schema_migrations table, so it runs once
per database. To change the schema, append a new function to MIGRATIONS and
never edit a released one. Migrations only add tables, columns and indexes:
existing rows are never rewritten.

Databases created before version tracking may already contain part of the
schema, so every migration must also be safe to run on such a database
(CREATE ... IF NOT EXISTS, add a column only when it is missing).
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .observability import event


class SchemaVersionError(RuntimeError):
    """The database was upgraded by a newer version of the application."""


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
        con.execute("PRAGMA journal_mode=WAL")
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
