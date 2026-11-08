"""Migration and actual JobStore query plans, using disposable synthetic history."""

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from document_processor import database
from document_processor.config import fingerprint
from document_processor.jobs import JobStore

INDEXES = {
    "jobs_by_created": ("created_at", "id"),
    "jobs_by_status_created": ("status", "created_at", "id"),
}


def populate(store, count):
    """Create deterministic ties and old active jobs at the end of physical insertion order."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    config = {
        "profile": "low",
        "max_attempts": 3,
        "system_prompt": "Synthetic private prompt",
    }
    configs, rows, runs, steps = [], [], [], []
    for number in range(1, count + 1):
        identifier = f"{number:032x}"
        status = (
            "queued"
            if number >= count - 1
            else "running"
            if number == count - 2
            else "done"
        )
        created = (
            "2000-01-01T00:00:00+00:00"
            if status != "done"
            else (base + timedelta(seconds=number // 2)).isoformat()
        )
        rows.append((identifier, "example.pdf", "Analyze", status, created, created))
        configs.append((identifier, json.dumps(config), fingerprint(config), created))
        for attempt in range(2):
            run_id = (number - 1) * 2 + attempt + 1
            runs.append((run_id, identifier, created))
            for offset in range(2):
                steps.append((run_id, identifier, f"analysis_{offset:04d}.md", created))
    with store.connect() as con:
        con.executemany(
            "INSERT INTO jobs (id,filename,question,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            rows,
        )
        con.executemany("INSERT INTO job_configs VALUES (?,?,?,?)", configs)
        con.executemany(
            "INSERT INTO job_runs (id,job_id,started_at,status) VALUES (?,?,?,'done')",
            runs,
        )
        con.executemany(
            "INSERT INTO job_steps (run_id,job_id,stage,step_key,started_at,status) "
            "VALUES (?,?,'section',?,?,'done')",
            steps,
        )


def capture_queries(store):
    """Capture SQL from the real methods, including transaction and deletion guards."""
    statements = []
    with store.connect() as con:
        con.set_trace_callback(statements.append)
    try:
        store.list_jobs(100)
        store.get_job(f"{1:032x}")
        store.metrics(f"{1:032x}")
        store.claim_next_job()
        store.delete_job(f"{2:032x}")
    finally:
        with store.connect() as con:
            con.set_trace_callback(None)
    return statements


def plans(store, statements):
    # Compile each before/after plan on the current schema, without cached EXPLAIN statements.
    with database.connect(store.db_path) as con:
        return {
            sql: [row["detail"] for row in con.execute("EXPLAIN QUERY PLAN " + sql)]
            for sql in dict.fromkeys(statements)
            if sql.startswith(("SELECT", "DELETE FROM jobs"))
        }


class JobIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = JobStore(self.root)
        self.addCleanup(self.store.close)
        # Isolate v4's indexes while retaining later schema used by current JobStore.
        without_indexes = tuple(item for item in database.MIGRATIONS if item[0] != 4)
        with patch.object(database, "MIGRATIONS", without_indexes):
            database.initialize(self.store.db_path)

    def contents(self):
        with self.store.connect() as con:
            return {
                table: [
                    tuple(row)
                    for row in con.execute(f"SELECT * FROM {table} ORDER BY 1")
                ]
                for table in ("jobs", "job_configs", "job_runs", "job_steps")
            }

    def test_upgrade_is_idempotent_and_preserves_every_historical_row_and_file(self):
        populate(self.store, 100)
        report = self.root / "jobs" / f"{1:032x}" / "previous_reports" / "retained.md"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"Historical report")
        before = self.contents()
        database.initialize(self.store.db_path)
        database.initialize(self.store.db_path)
        with self.store.connect() as con:
            # Even a database with pre-existing indexes but no v4 record upgrades safely.
            con.execute("DELETE FROM schema_migrations WHERE version=4")
        database.initialize(self.store.db_path)
        self.assertEqual(self.contents(), before)
        self.assertEqual(report.read_bytes(), b"Historical report")
        with self.store.connect() as con:
            self.assertEqual(
                [
                    row[0]
                    for row in con.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ],
                [version for version, _ in database.MIGRATIONS],
            )
            for index, columns in INDEXES.items():
                self.assertEqual(
                    tuple(
                        row["name"]
                        for row in con.execute(f"PRAGMA index_info({index})")
                    ),
                    columns,
                )
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_index_migration_rolls_back_both_index_and_version(self):
        populate(self.store, 100)
        before = self.contents()

        def broken(con):
            con.execute("CREATE INDEX jobs_by_created ON jobs(created_at, id)")
            raise sqlite3.OperationalError("Simulated index migration failure")

        with (
            patch.object(
                database,
                "MIGRATIONS",
                tuple(
                    (version, broken if version == 4 else migrate)
                    for version, migrate in database.MIGRATIONS
                ),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            database.initialize(self.store.db_path)
        with self.store.connect() as con:
            self.assertNotIn(
                "jobs_by_created",
                {row["name"] for row in con.execute("PRAGMA index_list(jobs)")},
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=4"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.contents(), before)
        database.initialize(self.store.db_path)
        self.assertEqual(self.contents(), before)

    def test_actual_query_plans_use_only_the_justified_new_indexes(self):
        populate(self.store, 1000)
        statements = capture_queries(self.store)
        before = plans(self.store, statements)
        database.initialize(self.store.db_path)
        after = plans(self.store, statements)
        for sql in before:
            details = " ".join(after[sql])
            if sql.startswith("SELECT jobs.*"):
                self.assertIn("USE TEMP B-TREE", " ".join(before[sql]))
                self.assertIn("jobs_by_created", details)
                self.assertNotIn("USE TEMP B-TREE", details)
            elif "status='queued' ORDER BY" in sql:
                self.assertIn("USE TEMP B-TREE", " ".join(before[sql]))
                self.assertIn("jobs_by_status_created", details)
                self.assertNotIn("USE TEMP B-TREE", details)
            elif "SELECT EXISTS" in sql:
                self.assertIn("jobs_by_status_created", details)
            elif "FROM job_runs" in sql:
                # Both cover (job_id, id); v5 adds uniqueness for revision ownership FKs.
                self.assertRegex(details, r"job_runs_(by_job|identity)")
            elif "FROM job_steps" in sql:
                self.assertIn("job_steps_by_job", details)
            elif sql.startswith(
                (
                    "SELECT * FROM jobs WHERE id=",
                    "SELECT status FROM jobs WHERE id=",
                    "DELETE FROM jobs",
                )
            ):
                self.assertIn("sqlite_autoindex_jobs_1", details)
        with self.store.connect() as con:
            added = {row["name"] for row in con.execute("PRAGMA index_list(jobs)")} - {
                "sqlite_autoindex_jobs_1"
            }
        self.assertEqual(added, set(INDEXES))
        self.assertIn("BEGIN IMMEDIATE", statements)

    def test_history_ties_active_flag_claim_order_and_metrics_are_unchanged(self):
        populate(self.store, 1000)
        before = self.store.list_jobs(100)
        point = self.store.get_job(f"{1:032x}")
        metrics = self.store.metrics(f"{1:032x}")
        self.assertTrue(before[1])
        self.assertTrue(all(job["status"] == "done" for job in before[0]))
        database.initialize(self.store.db_path)
        self.assertEqual(self.store.list_jobs(100), before)
        self.assertEqual(self.store.get_job(f"{1:032x}"), point)
        self.assertEqual(self.store.metrics(f"{1:032x}"), metrics)
        self.assertEqual(self.store.claim_next_job(), f"{999:032x}")
        self.assertEqual(self.store.claim_next_job(), f"{1000:032x}")
        self.assertIsNone(self.store.claim_next_job())


if __name__ == "__main__":
    unittest.main()
