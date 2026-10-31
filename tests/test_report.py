"""Report download HTTP contract, using only temporary files and SQLite."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ReportTests(unittest.TestCase):
    def setUp(self):
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
        content = "# Résumé\n\nAnalyse terminée.\n"
        (self.directory / "rapport_final.md").write_text(content, encoding="utf-8")
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.getlist("Content-Type"),
                             ["text/markdown; charset=utf-8"])
            self.assertEqual(response.headers["Content-Disposition"],
                             "attachment; filename=analysis_01234567.md")
            self.assertEqual(response.data, content.encode("utf-8"))

    def test_unfinished_jobs_cannot_download_a_report(self):
        for status in ("queued", "running", "failed"):
            with self.subTest(status=status):
                app.update_job(self.job_id, status=status)
                self.assertEqual(self.client.get(
                    f"/api/jobs/{self.job_id}/report").status_code, 409)

    def test_missing_job_or_report_returns_404(self):
        self.assertEqual(self.client.get(
            f"/api/jobs/{'f' * 32}/report").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/jobs/{self.job_id}/report").status_code, 404)


if __name__ == "__main__":
    unittest.main()
