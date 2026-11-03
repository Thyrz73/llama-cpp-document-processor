"""Test persisted progress and the jobs APIs."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import extraction


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
        for name, value in (("DATA", directory), ("DB_PATH", directory / "jobs.sqlite3")):
            patched = patch.object(app, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        app.init_db()
        self.client = app.app.test_client()

    def insert_job(self, job_id, status, percent):
        """Insert a raw job to simulate a legacy database.

        Write job_id, status, and percent directly through SQL to reproduce
        an older database without update_job normalization.
        """
        with app.db_conn() as con:
            con.execute("""INSERT INTO jobs
                (id, filename, question, status, progress_percent, created_at, updated_at)
                VALUES (?, 'example.pdf', 'Analyze', ?, ?, ?, ?)""",
                (job_id, status, percent, app.utcnow(), app.utcnow()))

    def stored_job(self, job_id):
        """Return raw values for the stored job."""
        with app.db_conn() as con:
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
                self.assertEqual(app.get_job(job_id)["progress_percent"], expected)
                self.assertEqual(self.stored_job(job_id)["progress_percent"], stored)
        self.assertEqual(self.client.get(f"/api/jobs/{'f' * 32}").status_code, 404)

    def test_marking_done_always_persists_100_percent(self):
        """Persist 100 percent on every successful completion."""
        job_id = "a" * 32
        for values in ({}, {"progress_percent": 0}, {"progress_percent": 95}):
            with self.subTest(values=values):
                with app.db_conn() as con:
                    con.execute("DELETE FROM jobs")
                self.insert_job(job_id, "running", 95)
                app.update_job(job_id, status="done", stage="Analysis completed", **values)
                stored = self.stored_job(job_id)
                self.assertEqual(stored["status"], "done")
                self.assertEqual(stored["progress_percent"], 100)

    def test_list_detects_active_jobs_outside_the_latest_100(self):
        """Keep polling when an older job is active outside the visible list."""
        self.assertEqual(self.client.get("/api/jobs").get_json(),
                         {"jobs": [], "has_active_jobs": False})
        for status in ("queued", "running"):
            with self.subTest(status=status):
                with app.db_conn() as con:
                    con.execute("DELETE FROM jobs")
                oldest_id = "0" * 32
                self.insert_job(oldest_id, status, 52)
                with app.db_conn() as con:
                    con.execute("UPDATE jobs SET created_at='2000-01-01' WHERE id=?",
                                (oldest_id,))
                for index in range(1, 101):
                    self.insert_job(f"{index:032x}", "done", 100)

                payload = self.client.get("/api/jobs").get_json()
                self.assertEqual(len(payload["jobs"]), 100)
                self.assertNotIn(oldest_id, [job["id"] for job in payload["jobs"]])
                self.assertTrue(payload["has_active_jobs"])

                app.update_job(oldest_id, status="failed")
                self.assertFalse(self.client.get("/api/jobs").get_json()["has_active_jobs"])

    def test_failure_and_retry_preserve_partial_progress(self):
        """Preserve progress after failure and retry."""
        job_id = "a" * 32
        self.insert_job(job_id, "running", 52)
        app.update_job(job_id, status="failed", stage="Error", error="Model unavailable")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").get_json()["progress_percent"], 52)
        with patch.object(app.worker_wakeup, "set") as wakeup:
            response = self.client.post(f"/api/jobs/{job_id}/retry")
        self.assertEqual(response.status_code, 200)
        wakeup.assert_called_once_with()
        stored = self.stored_job(job_id)
        self.assertEqual((stored["status"], stored["progress_percent"], stored["error"]),
                         ("queued", 52, ""))

    def test_workflow_progress_through_success_and_failure(self):
        """Test the complete workflow with and without failure.

        Run the real worker with a mocked model and extraction. Verify
        persisted stages, final state, and report existence; stop the loop
        with StopWorker once the queue is empty.
        """
        job_id = "b" * 32
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory(dir=self.tmp.name) as tmp:
                with app.db_conn() as con:
                    con.execute("DELETE FROM jobs")
                self.insert_job(job_id, "queued", 0)
                directory = Path(tmp)
                (directory / "source.pdf").write_bytes(b"%PDF-test")
                progress = []
                update = app.update_job

                def record_update(identifier, **values):
                    """Apply values and record progress."""
                    update(identifier, **values)
                    progress.append(self.stored_job(identifier)["progress_percent"])

                def extract(path, settings, progress):
                    """Simulate one page and call progress."""
                    progress("Extracting text: page 1/1")
                    return [{"page": 1, "text": "Example text"}], 1, [], []

                with patch.object(app, "read_system_prompt", return_value="Test instructions"), \
                     patch.object(app, "job_dir", return_value=directory), \
                     patch.object(app, "ModelClient") as model_class, \
                     patch.object(extraction, "extract_pdf", side_effect=extract), \
                     patch.object(extraction, "make_chunks", return_value=[
                         {"pages": [1], "text": "Example section"} for _ in range(5)]), \
                     patch.object(app, "update_job", side_effect=record_update), \
                     patch.object(app.worker_wakeup, "wait", side_effect=StopWorker):
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
                            app.worker_loop()
                    else:
                        with self.assertRaises(StopWorker):
                            app.worker_loop()
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
