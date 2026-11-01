"""Test Markdown downloads in isolated storage."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ReportTests(unittest.TestCase):
    """Test downloads with the Flask client."""

    def setUp(self):
        """Prepare temporary test resources.

        Redirect DATA and DB_PATH to a temporary directory. Register cleanup
        for files and patches even when the test fails.
        """
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        for name, value in (("DATA", directory), ("DB_PATH", directory / "jobs.sqlite3")):
            patched = patch.object(app, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        app.init_db()
        self.client = app.app.test_client()
        self.job_id = "0123456789abcdef" * 2
        with app.db_conn() as con:
            con.execute("""INSERT INTO jobs
                (id, filename, question, status, created_at, updated_at)
                VALUES (?, 'example.pdf', 'Analyze', 'done', ?, ?)""",
                (self.job_id, app.utcnow(), app.utcnow()))
        self.directory = app.job_dir(self.job_id)
        self.directory.mkdir(parents=True)

    def test_markdown_attachment_headers_filename_and_utf8_content(self):
        """Verify headers and UTF-8 Markdown content."""
        content = "# Summary\n\nAnalysis completed.\n"
        (self.directory / "rapport_final.md").write_text(content, encoding="utf-8")
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.getlist("Content-Type"),
                             ["text/markdown; charset=utf-8"])
            self.assertEqual(response.headers["Content-Disposition"],
                             "attachment; filename=analysis_01234567.md")
            self.assertEqual(response.data, content.encode("utf-8"))

    def test_unfinished_jobs_cannot_download_a_report(self):
        """Reject unfinished jobs with HTTP 409."""
        for status in ("queued", "running", "failed"):
            with self.subTest(status=status):
                app.update_job(self.job_id, status=status)
                self.assertEqual(self.client.get(
                    f"/api/jobs/{self.job_id}/report").status_code, 409)

    def test_missing_job_or_report_returns_404(self):
        """Require HTTP 404 when the job or report is missing."""
        self.assertEqual(self.client.get(
            f"/api/jobs/{'f' * 32}/report").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/jobs/{self.job_id}/report").status_code, 404)


if __name__ == "__main__":
    unittest.main()
