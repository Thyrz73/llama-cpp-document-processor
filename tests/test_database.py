"""Upgrade databases of every past release with numbered, atomic migrations, and share the connections."""

import gc
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from document_processor import database
from document_processor.jobs import JobStore

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


INSERT_JOB = ("INSERT INTO jobs (id, filename, question, status, created_at, updated_at) "
              "VALUES (?, 'a.pdf', 'Analyze', 'queued', '2025-01-01', '2025-01-01')")


class ThreadConnectionsTests(unittest.TestCase):
    """Each thread keeps one open connection, and a failed transaction never leaks into the next one."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "jobs.sqlite3"
        database.initialize(self.path)
        self.connections = database.ThreadConnections(self.path)
        self.addCleanup(self.connections.close)

    def job_ids(self):
        with self.connections.transaction() as con:
            return [row["id"] for row in con.execute("SELECT id FROM jobs ORDER BY id")]

    def test_a_thread_reuses_its_connection_and_each_thread_has_its_own(self):
        with self.connections.transaction() as first:
            pass
        with self.connections.transaction() as second:
            pass
        self.assertIs(first, second)
        other = []

        def use():
            with self.connections.transaction() as con:
                other.append(id(con))
            self.connections.close()

        thread = threading.Thread(target=use)
        thread.start()
        thread.join()
        self.assertNotEqual(other, [id(first)])

    def test_commits_skip_the_disk_wait_only_in_wal_mode(self):
        with self.connections.transaction() as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
        # Without WAL, NORMAL could corrupt the database after a power failure: keep the default.
        self.connections.close()
        with database.connect(self.path) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        with database.connect(self.path) as con:
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL

    def test_an_error_rolls_back_and_keeps_the_connection(self):
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            with self.connections.transaction() as con:
                con.execute(INSERT_JOB, ("a",))
                raise RuntimeError("simulated failure")
        self.assertEqual(self.job_ids(), [])
        with self.connections.transaction() as again:
            self.assertIs(again, con)

    def test_a_sqlite_error_replaces_the_connection(self):
        with self.connections.transaction() as con:
            con.execute(INSERT_JOB, ("a",))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connections.transaction() as failing:
                failing.execute(INSERT_JOB, ("b",))
                failing.execute(INSERT_JOB, ("a",))  # the primary key exists
        with self.assertRaises(sqlite3.ProgrammingError):
            failing.execute("SELECT 1")  # closed
        self.assertEqual(self.job_ids(), ["a"])
        with self.connections.transaction() as replacement:
            self.assertIsNot(replacement, failing)

    def test_a_dropped_connection_is_closed_explicitly(self):
        closed = []
        real_close = sqlite3.Connection.close

        def spy(connection):
            closed.append(True)
            real_close(connection)

        # Python 3.13 warns about a connection that was never closed: nothing closes the ones of web server threads.
        connections = database.ThreadConnections(self.path)
        with patch.object(database._Connection, "close", spy):
            with connections.transaction():
                pass
            del connections
            gc.collect()  # a connection is part of a reference cycle
        self.assertEqual(closed, [True])

    def test_transactions_cannot_be_nested(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be nested"):
            with self.connections.transaction() as con:
                con.execute(INSERT_JOB, ("a",))
                with self.connections.transaction():
                    pass
        # The refused block left nothing behind: the outer one was rolled back.
        self.assertEqual(self.job_ids(), [])

    def test_close_folds_the_log_into_the_database_file_while_another_thread_stays_connected(self):
        connected, release = threading.Event(), threading.Event()

        def stay_connected():
            with self.connections.transaction():
                pass
            connected.set()
            release.wait(30)
            self.connections.close()

        other = threading.Thread(target=stay_connected)
        other.start()
        self.addCleanup(other.join)
        self.addCleanup(release.set)
        self.assertTrue(connected.wait(30))
        with self.connections.transaction() as con:
            con.execute(INSERT_JOB, ("a",))
        self.connections.close()
        # The other connection is still open, so SQLite did not fold the log in by itself.
        self.assertEqual(self.path.with_name(self.path.name + "-wal").stat().st_size, 0)
        copy = self.path.with_name("copy.sqlite3")
        shutil.copy(self.path, copy)
        with closing(sqlite3.connect(copy)) as check:
            self.assertEqual(check.execute("SELECT id FROM jobs").fetchall(), [("a",)])
        self.connections.close()  # a second call has nothing to do

    def test_web_and_worker_threads_share_one_store_without_lock_errors(self):
        store = JobStore(self.path.parent)
        self.addCleanup(store.close)
        identifiers = [f"{index:032x}" for index in range(4)]
        for identifier in identifiers:
            store.create_job(identifier, "a.pdf", "Analyze", 1, {"max_attempts": 3})
        failures, finished = [], threading.Event()

        def write(identifier):
            try:
                for percent in range(1, 41):
                    store.update_job(identifier, progress_percent=percent)
            except Exception as exc:
                failures.append(exc)
            finally:
                store.close()

        def read():
            try:
                while not finished.is_set():
                    store.list_jobs(10)
                    store.get_job(identifiers[0])
                    store.read_cancellation(identifiers[1])
            except Exception as exc:
                failures.append(exc)
            finally:
                store.close()

        with patch("document_processor.jobs.event"):  # keep the log of 160 updates out of the test output
            writers = [threading.Thread(target=write, args=(identifier,)) for identifier in identifiers]
            readers = [threading.Thread(target=read) for _ in range(3)]
            for thread in readers + writers:
                thread.start()
            for thread in writers:
                thread.join()
            finished.set()
            for thread in readers:
                thread.join()
        self.assertEqual(failures, [])
        self.assertEqual([store.get_job(identifier)["progress_percent"] for identifier in identifiers], [40] * 4)


if __name__ == "__main__":
    unittest.main()
