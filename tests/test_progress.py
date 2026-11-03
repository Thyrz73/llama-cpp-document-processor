"""Test persisted progress and the jobs APIs."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import extraction, jobs
from document_processor.jobs import JobStore
from document_processor.worker import IDLE_SECONDS, Worker


class StopWorker(BaseException):
    """Stop the worker loop after its queue has been processed.

    Inherit from BaseException so the signal passes through the loop's
    ``except Exception`` blocks without turning test shutdown into a job
    failure.
    """


class ProgressTests(unittest.TestCase):
    """Verify job states and progress."""

    def setUp(self):
        """Prepare temporary test resources."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        patched = patch.object(app, "store", JobStore(directory))
        patched.start()
        self.addCleanup(patched.stop)
        app.store.initialize()
        self.client = app.app.test_client()

    def insert_job(self, job_id, status, percent):
        """Insert a raw job to simulate a legacy database.

        Write job_id, status, and percent directly through SQL to reproduce
        an older database without update_job normalization.
        """
        with app.store.connect() as con:
            con.execute("""INSERT INTO jobs
                (id, filename, question, status, progress_percent, created_at, updated_at)
                VALUES (?, 'example.pdf', 'Analyze', ?, ?, ?, ?)""",
                (job_id, status, percent, jobs.utcnow(), jobs.utcnow()))

    def stored_job(self, job_id):
        """Return raw values for the stored job."""
        with app.store.connect() as con:
            return dict(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def test_list_and_detail_report_consistent_progress_for_all_statuses(self):
        """Verify APIs without rewriting jobs."""
        cases = [("queued", 0, 0), ("queued", 52, 52),
                 ("running", 2, 2), ("running", 52, 52), ("running", 95, 95),
                 ("failed", 0, 0), ("failed", 52, 52), ("failed", 95, 95),
                 ("done", 0, 100), ("done", 52, 100), ("done", 100, 100)]
        for index, (status, stored, expected) in enumerate(cases):
            self.insert_job(f"{index:032x}", status, stored)
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        listed = {job["id"]: job for job in response.get_json()["jobs"]}
        for index, (status, stored, expected) in enumerate(cases):
            job_id = f"{index:032x}"
            with self.subTest(status=status, stored=stored):
                response = self.client.get(f"/api/jobs/{job_id}")
                self.assertEqual(response.status_code, 200)
                detail = response.get_json()
                self.assertEqual(detail, listed[job_id])
                self.assertEqual(detail["status"], status)
                self.assertEqual(detail["progress_percent"], expected)
                self.assertEqual(app.store.get_job(job_id)["progress_percent"], expected)
                self.assertEqual(self.stored_job(job_id)["progress_percent"], stored)
        self.assertEqual(self.client.get(f"/api/jobs/{'f' * 32}").status_code, 404)

    def test_marking_done_always_persists_100_percent(self):
        """Persist 100 percent on every successful completion."""
        job_id = "a" * 32
        for values in ({}, {"progress_percent": 0}, {"progress_percent": 95}):
            with self.subTest(values=values):
                with app.store.connect() as con:
                    con.execute("DELETE FROM jobs")
                self.insert_job(job_id, "running", 95)
                app.store.update_job(job_id, status="done", stage="Analysis completed", **values)
                stored = self.stored_job(job_id)
                self.assertEqual(stored["status"], "done")
                self.assertEqual(stored["progress_percent"], 100)

    def test_list_detects_active_jobs_outside_the_latest_100(self):
        """Keep polling when an older job is active outside the visible list."""
        self.assertEqual(self.client.get("/api/jobs").get_json(),
                         {"jobs": [], "has_active_jobs": False})
        for status in ("queued", "running"):
            with self.subTest(status=status):
                with app.store.connect() as con:
                    con.execute("DELETE FROM jobs")
                oldest_id = "0" * 32
                self.insert_job(oldest_id, status, 52)
                with app.store.connect() as con:
                    con.execute("UPDATE jobs SET created_at='2000-01-01' WHERE id=?",
                                (oldest_id,))
                for index in range(1, 101):
                    self.insert_job(f"{index:032x}", "done", 100)

                payload = self.client.get("/api/jobs").get_json()
                self.assertEqual(len(payload["jobs"]), 100)
                self.assertNotIn(oldest_id, [job["id"] for job in payload["jobs"]])
                self.assertTrue(payload["has_active_jobs"])

                app.store.update_job(oldest_id, status="failed")
                self.assertFalse(self.client.get("/api/jobs").get_json()["has_active_jobs"])

    def test_failure_and_retry_preserve_partial_progress(self):
        """Preserve progress after failure and retry."""
        job_id = "a" * 32
        self.insert_job(job_id, "running", 52)
        app.store.update_job(job_id, status="failed", stage="Error", error="Model unavailable")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").get_json()["progress_percent"], 52)
        with patch.object(app.worker, "wake") as wakeup:
            response = self.client.post(f"/api/jobs/{job_id}/retry")
        self.assertEqual(response.status_code, 200)
        wakeup.assert_called_once_with()
        stored = self.stored_job(job_id)
        self.assertEqual((stored["status"], stored["progress_percent"], stored["error"]),
                         ("queued", 52, ""))

    def test_retry_limit_and_unknown_jobs_are_refused(self):
        """Refuse a retry once every attempt is used, and any action on an unknown job."""
        job_id = "c" * 32
        self.insert_job(job_id, "failed", 52)
        app.store.start_attempt(job_id, jobs.utcnow()).finish("failed")
        # A job saved without a configuration snapshot is limited by MAX_JOB_ATTEMPTS.
        with patch.object(app, "MAX_JOB_ATTEMPTS", 1), patch.object(app.worker, "wake") as wakeup:
            response = self.client.post(f"/api/jobs/{job_id}/retry")
        self.assertEqual((response.status_code, response.get_json()["error_code"]), (409, "retry_limit"))
        wakeup.assert_not_called()
        self.assertEqual(self.stored_job(job_id)["status"], "failed")
        unknown = "d" * 32
        for method, action in (("post", "cancel"), ("post", "retry"), ("get", "live"),
                               ("get", "metrics"), ("get", "report")):
            with self.subTest(action=action):
                response = getattr(self.client, method)(f"/api/jobs/{unknown}/{action}")
                self.assertEqual(response.status_code, 404)

    def test_restart_requeues_interrupted_jobs_and_applies_pending_cancellations(self):
        """Recover the jobs and attempts that a crash or a restart left running."""
        interrupted, cancelling, cancelled, waiting = (f"{number:032x}" for number in range(1, 5))
        self.insert_job(interrupted, "running", 52)
        self.insert_job(cancelling, "running", 52)
        self.insert_job(cancelled, "queued", 0)
        self.insert_job(waiting, "queued", 0)
        app.store.update_job(cancelling, cancel_requested=1)
        app.store.update_job(cancelled, cancel_requested=1)
        app.store.start_attempt(interrupted, jobs.utcnow()).start("section", "section-1")
        untouched = self.stored_job(waiting)

        app.store.initialize()

        stored = self.stored_job(interrupted)
        self.assertEqual((stored["status"], stored["stage"], stored["progress_percent"]),
                         ("queued", "Resuming after restart", 52))
        for job_id in (cancelling, cancelled):
            self.assertEqual(self.stored_job(job_id)["status"], "cancelled")
        self.assertEqual(self.stored_job(waiting), untouched)
        # The cut-short attempt stays in the history instead of looking active forever.
        history = app.store.metrics(interrupted)
        self.assertEqual([run["status"] for run in history["runs"]], ["interrupted"])
        self.assertEqual([step["status"] for step in history["steps"]], ["interrupted"])

    def test_database_errors_neither_stop_nor_spin_the_worker(self):
        """Log a failed queue read, then wait IDLE_SECONDS before trying again."""
        runner = Worker(app.store, app.analyze_job)
        with patch.object(app.store, "claim_next_job", side_effect=sqlite3.OperationalError("database is locked")), \
             patch.object(runner._wakeup, "wait", side_effect=[False, StopWorker]) as wait, \
             self.assertLogs(app.log, level="ERROR") as logs, self.assertRaises(StopWorker):
            runner.run_forever()
        self.assertEqual(wait.call_count, 2)
        wait.assert_called_with(timeout=IDLE_SECONDS)
        self.assertEqual(sum("worker_database_error" in line for line in logs.output), 2)

    def test_workflow_progress_through_success_and_failure(self):
        """Test the complete workflow with and without failure.

        Run the real worker with a mocked model and extraction. Verify
        persisted stages, final state, and report existence; stop the loop
        with StopWorker once the queue is empty.
        """
        job_id = "b" * 32
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory(dir=self.tmp.name) as tmp:
                with app.store.connect() as con:
                    con.execute("DELETE FROM jobs")
                self.insert_job(job_id, "queued", 0)
                directory = Path(tmp)
                (directory / "source.pdf").write_bytes(b"%PDF-test")
                progress = []
                update = app.store.update_job

                def record_update(identifier, **values):
                    """Apply values and record progress."""
                    update(identifier, **values)
                    progress.append(self.stored_job(identifier)["progress_percent"])

                def extract(path, settings, progress):
                    """Simulate one page and call progress."""
                    progress("Extracting text: page 1/1")
                    return [{"page": 1, "text": "Example text"}], 1, [], []

                runner = Worker(app.store, app.analyze_job)
                with patch.object(app, "read_system_prompt", return_value="Test instructions"), \
                     patch.object(app.store, "job_dir", return_value=directory), \
                     patch.object(app, "ModelClient") as model_class, \
                     patch.object(extraction, "extract_pdf", side_effect=extract), \
                     patch.object(extraction, "make_chunks", return_value=[
                         {"pages": [1], "text": "Example section"} for _ in range(5)]), \
                     patch.object(app.store, "update_job", side_effect=record_update), \
                     patch.object(runner._wakeup, "wait", side_effect=StopWorker):
                    model = model_class.return_value
                    model.tokenize.return_value = 10
                    model.inspect.return_value = {"context_tokens": 8192, "model": "test"}
                    model.model.return_value = "test"
                    model.fits.return_value = (True, 10, "estimated_template", 384)
                    model.current_metrics = {}
                    model.tokenization_calls = 0
                    model.tokenization_ms = 0
                    model.tokenization_cache_hits = 0
                    # Simulate five sections, one merge, and the final report.
                    model.complete.side_effect = ["Notes [Page 1]"] * 6 + [
                        RuntimeError("Model unavailable") if fail else "Final report [Page 1]"]
                    if fail:
                        with self.assertLogs(app.log, level="ERROR"), self.assertRaises(StopWorker):
                            runner.run_forever()
                    else:
                        with self.assertRaises(StopWorker):
                            runner.run_forever()
                    self.assertEqual(model.complete.call_count, 7)

                self.assertEqual(progress, sorted(progress))
                for milestone in (2, 7, 10, 80, 81, 95):
                    self.assertIn(milestone, progress)
                expected = ("failed", 95) if fail else ("done", 100)
                stored = self.stored_job(job_id)
                self.assertEqual((stored["status"], stored["progress_percent"]), expected)
                self.assertEqual((directory / "rapport_final.md").exists(), not fail)
                detail = self.client.get(f"/api/jobs/{job_id}").get_json()
                self.assertEqual((detail["status"], detail["progress_percent"]), expected)


if __name__ == "__main__":
    unittest.main()
