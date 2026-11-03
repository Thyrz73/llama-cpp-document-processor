"""Test Markdown downloads in isolated storage."""
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import jobs, reports
from document_processor.jobs import JobStore


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class ReportTests(unittest.TestCase):
    """Test downloads with the Flask client."""

    def setUp(self):
        """Prepare temporary test resources.

        Use a job store in a temporary directory. Register cleanup
        for files and patches even when the test fails.
        """
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        patched = patch.object(app, "store", JobStore(directory))
        patched.start()
        self.addCleanup(patched.stop)
        app.store.initialize()
        self.client = app.app.test_client()
        self.job_id = "0123456789abcdef" * 2
        with app.store.connect() as con:
            con.execute("""INSERT INTO jobs
                (id, filename, question, status, created_at, updated_at)
                VALUES (?, 'example.pdf', 'Analyze', 'done', ?, ?)""",
                (self.job_id, jobs.utcnow(), jobs.utcnow()))
        self.directory = app.store.job_dir(self.job_id)
        self.directory.mkdir(parents=True)

    def test_markdown_attachment_headers_filename_and_utf8_content(self):
        """Verify headers and UTF-8 Markdown content."""
        content = "# Summary\n\nAnalysis completed — temperature: 25 °C ± 0.5 °C.\n"
        (self.directory / "rapport_final.md").write_text(content, encoding="utf-8")
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.getlist("Content-Type"),
                             ["text/markdown; charset=utf-8"])
            self.assertEqual(response.headers["Content-Disposition"],
                             "attachment; filename=example_rapport.md; filename*=UTF-8''example_rapport.md")
            self.assertEqual(response.data, content.encode("utf-8"))

    def test_unfinished_jobs_cannot_download_a_report(self):
        """Reject unfinished jobs with HTTP 409."""
        for status in ("queued", "running", "failed"):
            with self.subTest(status=status):
                app.store.update_job(self.job_id, status=status)
                self.assertEqual(self.client.get(
                    f"/api/jobs/{self.job_id}/report").status_code, 409)

    def test_missing_job_or_report_returns_404(self):
        """Require HTTP 404 when the job or report is missing."""
        self.assertEqual(self.client.get(
            f"/api/jobs/{'f' * 32}/report").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/jobs/{self.job_id}/report").status_code, 404)

    def write_report(self, text):
        (self.directory / "rapport_final.md").write_text(text, encoding="utf-8")

    def test_preview_renders_structure_unicode_and_downloads_identical_bytes(self):
        text = ("# Étude des bâtiments\n\n## Données\n\nL’été à Grenoble.\nDeuxième ligne.\n\n"
                "- Élément\n\n> Citation\n\n| Lieu | Valeur |\n| --- | --- |\n| Paris | 25 °C |\n\n"
                "[Source](https://example.org)\n\n```python\nprint('é')\n```\n")
        self.write_report(text)
        with patch.object(app, "ModelClient", side_effect=AssertionError("No model call")):
            preview = self.client.get(f"/api/jobs/{self.job_id}/report/view?lang=fr")
            self.assertEqual(preview.status_code, 200)
            body = preview.get_data(as_text=True)
            for markup in ("<h1>Étude des bâtiments</h1>", "<h2>", "<ul>", "<blockquote>", "<table>", "<pre><code", "<br"):
                self.assertIn(markup, body)
            self.assertIn("Télécharger", body)
            self.assertIn("Etude_des_batiments_rapport.md", body)
            self.assertIn("default-src 'none'", preview.headers["Content-Security-Policy"])
            self.assertEqual(preview.headers["Cache-Control"], "no-store")
            with self.client.get(f"/api/jobs/{self.job_id}/report") as download:
                self.assertEqual(download.data, text.encode("utf-8"))
        # Refreshing the preview requires no browser session or transient JavaScript state.
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/report/view").status_code, 200)
        self.assertEqual(app.store.get_job(self.job_id)["report_name"], "Etude_des_batiments_rapport.md")
        self.assertEqual(list(self.directory.glob("*.md")), [self.directory / "rapport_final.md"])

    def test_untrusted_html_images_and_executable_links_are_inert(self):
        self.write_report('''# Security report
<script>alert(1)</script><img src=x onerror=alert(1)>
<iframe src="https://example.org"></iframe>

[bad](javascript:alert%281%29) [bad](vbscript:msgbox) [bad](data:text/html,test)
[bad](file:///etc/passwd) [bad](custom:run)
![tracking](https://example.org/pixel.png)
[safe](https://example.org)
''')
        response = self.client.get(f"/api/jobs/{self.job_id}/report/view")
        tags = Tags(response.get_data(as_text=True)).tags
        self.assertFalse(any(tag in ("img", "iframe", "object", "embed") for tag, _ in tags))
        scripts = [attrs for tag, attrs in tags if tag == "script"]
        self.assertEqual(scripts, [{"src": "/static/report.js"}])
        for _, attrs in tags:
            self.assertFalse(any(key.startswith("on") for key in attrs))
            self.assertFalse(attrs.get("href", "").startswith(("javascript:", "vbscript:", "data:", "file:", "custom:")))

    def test_empty_and_oversized_previews_keep_original_download_available(self):
        self.write_report("")
        response = self.client.get(f"/api/jobs/{self.job_id}/report/view?lang=fr")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Aucun contenu disponible.", response.get_data(as_text=True))
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.data, b"")
        self.write_report("a" * (reports.PREVIEW_MAX_BYTES + 1))
        with patch.object(reports, "render_markdown", side_effect=AssertionError("Oversized preview parsed")):
            response = self.client.get(f"/api/jobs/{self.job_id}/report/view")
        self.assertEqual(response.status_code, 413)
        self.assertIn("Download the original Markdown", response.get_data(as_text=True))
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(len(response.data), reports.PREVIEW_MAX_BYTES + 1)

    def test_missing_unfinished_invalid_and_private_artifacts(self):
        response = self.client.get(f"/api/jobs/{self.job_id}/report/view?lang=fr")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Rapport introuvable", response.get_data(as_text=True))
        self.write_report("# Old report")
        app.store.update_job(self.job_id, status="running")
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/report/view").status_code, 409)
        app.store.update_job(self.job_id, status="done")
        for path in ("not-an-id/report", "not-an-id/report/view", f"{self.job_id}/system_prompt.txt",
                     f"{self.job_id}/report/../../source.pdf", f"{self.job_id}/report/artifacts/manifest.json"):
            self.assertEqual(self.client.get("/api/jobs/" + path).status_code, 404)

    def test_symlinked_reports_and_job_roots_are_rejected(self):
        outside = Path(self.tmp.name) / "private.md"
        outside.write_text("SECRET", encoding="utf-8")
        (self.directory / "rapport_final.md").symlink_to(outside)
        for suffix in ("report", "report/view"):
            self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/{suffix}").status_code, 404)
        (self.directory / "rapport_final.md").unlink()
        self.directory.rmdir()
        self.directory.symlink_to(outside.parent, target_is_directory=True)
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/report").status_code, 404)

    def test_listing_never_offers_symlinked_reports_or_job_roots(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "rapport_final.md").write_text("SECRET", encoding="utf-8")

        def listed():
            response = self.client.get("/api/jobs")
            self.assertEqual(response.status_code, 200)
            return response.json["jobs"][0]["report_available"]

        (self.directory / "rapport_final.md").symlink_to(outside / "rapport_final.md")
        self.assertFalse(listed())
        (self.directory / "rapport_final.md").unlink()
        self.directory.rmdir()
        # A symlinked job root holding a regular report used to make the whole list fail with 404.
        self.directory.symlink_to(outside, target_is_directory=True)
        self.assertFalse(listed())
        self.directory.unlink()
        self.directory.mkdir()
        self.write_report("# Report")
        self.assertTrue(listed())

    def test_listing_reads_no_report_content_and_legacy_name_is_cached(self):
        self.write_report("# Rapport final\n\n## Étude géotechnique du site A")
        with patch.object(Path, "read_text", side_effect=AssertionError("List read a report")), \
             patch.object(reports, "report_filename", side_effect=AssertionError("List parsed Markdown")):
            jobs = self.client.get("/api/jobs").json["jobs"]
        self.assertTrue(jobs[0]["report_available"])
        self.assertIsNone(jobs[0]["report_name"])
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertIn("Etude_geotechnique_du_site_a_rapport.md", response.headers["Content-Disposition"])
        with patch.object(reports, "report_filename", side_effect=AssertionError("Name computed twice")):
            with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
                self.assertEqual(response.status_code, 200)
        (self.directory / "rapport_final.md").unlink()
        self.assertFalse(self.client.get("/api/jobs").json["jobs"][0]["report_available"])


class NamingTests(unittest.TestCase):
    def test_descriptive_heading_and_generic_prefixes(self):
        cases = [
            ("# Analyse du Document : Consommation Énergétique des Bâtiments (2025–2026)",
             "Consommation_energetique_des_batiments_2025-2026_rapport.md"),
            ("# Analyse de fichier.pdf\n## Rapport final\n### Consommation énergétique des bâtiments",
             "Consommation_energetique_des_batiments_rapport.md"),
            ("# Final report: **Geothermal** `measurements`", "Geothermal_measurements_rapport.md"),
            ("Étude des sols\n==============", "Etude_des_sols_rapport.md"),
        ]
        for markdown, expected in cases:
            with self.subTest(markdown=markdown):
                self.assertEqual(reports.report_filename(markdown, "input.pdf"), expected)

    def test_fallback_portability_length_and_malformed_titles(self):
        generic = "# Analyse du document\n## Rapport final\n## Summary\n## Introduction\n## 12345\n"
        for markdown in ("", generic, "```md\n# Fake heading\n```", "# 😀"):
            self.assertEqual(reports.report_filename(markdown, "Etude_geotechnique_site_A.pdf"),
                             "Etude_geotechnique_site_A_rapport.md")
        for title in ("# ", "# <>:\"/\\|?*\x00", "# CON", "# " + "Éléphants " * 300, "# 中文"):
            name = reports.report_filename(title, "../étrange\x00:?*\\AUX.pdf")
            self.assertLessEqual(len(name), 100)
            self.assertRegex(name, r"^[A-Za-z0-9_-]+_rapport\.md$")
        self.assertEqual(reports.report_filename("", "???"), "document_rapport.md")


if __name__ == "__main__":
    unittest.main()
