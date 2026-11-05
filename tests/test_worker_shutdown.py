"""Bounded worker shutdown, connection ownership and transaction recovery without a model."""

import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from document_processor import database
from document_processor.jobs import JobStore
from document_processor.worker import Worker


class WorkerShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="worker-shutdown-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = JobStore(self.root)
        self.store.initialize()
        self.addCleanup(self.store.close)

    def create(self, number=1):
        identifier = f"{number:032x}"
        self.store.create_job(
            identifier, "test.pdf", "Summarize", 1, {"max_attempts": 3}
        )
        return identifier

    def test_idle_worker_wakes_for_shutdown_and_closes_its_own_connection_only(self):
        idle, closed = threading.Event(), []
        original_claim, original_close = self.store.claim_next_job, self.store.close
        with self.store.connect() as main:
            pass

        def claim():
            result = original_claim()
            idle.set()
            return result

        def close(**kwargs):
            with self.store.connect() as con:
                pass
            original_close(**kwargs)
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                con.execute("SELECT 1")
            closed.append((threading.get_ident(), kwargs))

        runner = Worker(self.store, lambda *args: self.fail("No job should run"))
        with (
            patch.object(self.store, "claim_next_job", side_effect=claim),
            patch.object(self.store, "close", side_effect=close),
        ):
            runner.start()
            try:
                self.assertTrue(idle.wait(3))
                self.assertTrue(runner.stop(timeout=1))
                self.assertTrue(runner.stop(timeout=0))
            finally:
                runner.stop(timeout=1)
        self.assertEqual(closed, [(runner._thread.ident, {"checkpoint": False})])
        with self.store.connect() as same:
            self.assertIs(same, main)

    def test_worker_keeps_one_connection_across_jobs_until_shutdown(self):
        identifiers = [self.create(number) for number in (1, 2)]
        completed, connections = threading.Event(), []

        def run(identifier, check):
            with self.store.connect() as con:
                connections.append(id(con))
            self.store.update_job(identifier, status="done")
            if len(connections) == 2:
                completed.set()

        runner = Worker(self.store, run)
        with patch.object(database, "_open", wraps=database._open) as opens:
            runner.start()
            try:
                self.assertTrue(completed.wait(3))
                self.assertTrue(runner.stop(timeout=1))
            finally:
                runner.stop(timeout=1)
            self.assertEqual(
                opens.call_count, 1, "Shutdown must not undo P1 connection reuse"
            )
        self.assertEqual(len(set(connections)), 1)
        self.assertEqual(
            [self.store.get_job(identifier)["status"] for identifier in identifiers],
            ["done", "done"],
        )

    def test_shutdown_during_transaction_commits_or_rolls_back_before_owner_closes(
        self,
    ):
        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                identifier = self.create(10 if rollback else 20)
                waiting = self.create(11 if rollback else 21)
                entered = threading.Event()

                def run(job_id, check):
                    self.store.update_job(job_id, progress_percent=20)
                    with self.store.connect() as con:
                        con.execute(
                            "UPDATE jobs SET progress_percent=41 WHERE id=?", (job_id,)
                        )
                        entered.set()
                        if not runner._stopping.wait(3):
                            raise RuntimeError("Shutdown was not requested")
                        if rollback:
                            check(job_id)
                    check(job_id)

                runner = Worker(self.store, run)
                runner.start()
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertTrue(runner.stop(timeout=1))
                finally:
                    runner.stop(timeout=1)
                job = self.store.get_job(identifier)
                self.assertEqual(
                    (job["status"], job["progress_percent"], job["cancel_requested"]),
                    ("running", 20 if rollback else 41, 0),
                )
                self.assertEqual(self.store.get_job(waiting)["status"], "queued")
                self.store.close()
                copy = self.root / "copy.sqlite3"
                shutil.copy(self.store.db_path, copy)
                with closing(sqlite3.connect(copy)) as con:
                    self.assertEqual(
                        con.execute(
                            "SELECT progress_percent FROM jobs WHERE id=?",
                            (identifier,),
                        ).fetchone()[0],
                        20 if rollback else 41,
                    )
                self.store.initialize()
                self.assertEqual(self.store.get_job(identifier)["status"], "queued")
                # Keep the next subcase's queue empty.
                for job_id in (identifier, waiting):
                    self.store.update_job(job_id, status="done")

    def test_non_cooperative_worker_times_out_without_cross_thread_connection_close(
        self,
    ):
        identifier = self.create()
        entered, release = threading.Event(), threading.Event()

        def run(job_id, check):
            self.store.update_job(job_id, progress_percent=20)
            with self.store.connect() as con:
                con.execute("UPDATE jobs SET progress_percent=80 WHERE id=?", (job_id,))
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Test transaction was not released")
            check(job_id)

        runner = Worker(self.store, run)
        runner.start()
        try:
            self.assertTrue(entered.wait(3))
            started = time.monotonic()
            with self.assertLogs("document-processor", "WARNING") as logs:
                self.assertFalse(runner.stop(timeout=0.02))
                with patch.object(database, "CHECKPOINT_TIMEOUT_MS", 30):
                    self.store.close()
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(runner._thread.is_alive())
            self.assertTrue(
                any("worker_shutdown_timeout" in line for line in logs.output)
            )
            self.assertTrue(any('status="busy"' in line for line in logs.output))
        finally:
            release.set()
            self.assertTrue(runner.stop(timeout=2))
        job = self.store.get_job(identifier)
        self.assertEqual((job["status"], job["progress_percent"]), ("running", 80))
        self.store.initialize()
        self.assertEqual(self.store.get_job(identifier)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
