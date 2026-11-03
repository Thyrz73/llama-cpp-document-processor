"""Upgrade databases of every past release with numbered, atomic migrations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processor import database

LEGACY_JOBS = """CREATE TABLE jobs (
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
    status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', pages INTEGER DEFAULT 0,
    total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
    error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "jobs.sqlite3"

    def versions(self):
        with database.connect(self.path) as con:
            return [row[0] for row in con.execute("SELECT version FROM schema_migrations ORDER BY version")]

    def columns(self, table):
        with database.connect(self.path) as con:
            return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}

    def test_new_database_gets_every_version_once(self):
        database.initialize(self.path)
        database.initialize(self.path)
        self.assertEqual(self.versions(), [version for version, _ in database.MIGRATIONS])
        self.assertTrue({"progress_percent", "cancel_requested", "report_name", "question_custom"} <= self.columns("jobs"))
        self.assertIn("step_key", self.columns("job_steps"))

    def test_first_release_database_is_upgraded_without_touching_rows(self):
        with sqlite3.connect(self.path) as con:
            con.execute(LEGACY_JOBS)
            con.execute("INSERT INTO jobs (id, filename, question, status, created_at, updated_at) "
                        "VALUES ('a', 'old.pdf', 'Old request', 'done', '2025-01-01', '2025-01-01')")
        database.initialize(self.path)
        self.assertEqual(self.versions(), [1, 2, 3])
        with database.connect(self.path) as con:
            row = dict(con.execute("SELECT * FROM jobs").fetchone())
        self.assertEqual((row["question"], row["updated_at"], row["progress_percent"]), ("Old request", "2025-01-01", 0))
        self.assertIsNone(row["question_custom"])

    def test_database_from_releases_recording_versions_2_and_3_only(self):
        # Releases before numbered migrations recorded versions 2 and 3 but never 1.
        database.initialize(self.path)
        with database.connect(self.path) as con:
            con.execute("DELETE FROM schema_migrations WHERE version=1")
        database.initialize(self.path)
        self.assertEqual(self.versions(), [1, 2, 3])

    def test_newer_schema_refuses_to_start(self):
        database.initialize(self.path)
        with database.connect(self.path) as con:
            con.execute("INSERT INTO schema_migrations VALUES (999, 'future')")
        with self.assertRaisesRegex(database.SchemaVersionError, "newer than this application"):
            database.initialize(self.path)

    def test_failed_migration_is_rolled_back_and_retried_at_next_start(self):
        def broken(con):
            con.execute("ALTER TABLE jobs ADD COLUMN half_done TEXT")
            raise sqlite3.OperationalError("simulated failure")

        with patch.object(database, "MIGRATIONS", database.MIGRATIONS + ((4, broken),)):
            with self.assertRaises(sqlite3.OperationalError):
                database.initialize(self.path)
        self.assertEqual(self.versions(), [1, 2, 3])
        self.assertNotIn("half_done", self.columns("jobs"))


if __name__ == "__main__":
    unittest.main()
