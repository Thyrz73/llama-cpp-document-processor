"""Test Markdown downloads in isolated storage."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import jobs, prompts, reports
from document_processor.application import create_app
from document_processor.jobs import JobStore
from document_processor.web import reports as web_reports


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class ReportLanguageTests(unittest.TestCase):
    def test_auto_uses_request_words_and_defaults_to_english_when_ambiguous(self):
        cases = [
            ("Résumez les résultats et les méthodes de ce document.", "fr"),
            ("Analyse ce document.", "fr"),
            ("Analyse this document.", "en"),
            ("Summarize the results and methods of this document.", "en"),
            ("le the", "en"),
            ("PDF 2026", "en"),
            ("中文", "en"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(
                    prompts.resolve_report_language("auto", question), expected
                )

    def test_explicit_languages_and_existing_french_aliases_take_precedence(self):
        for language in ("fr", "French", "français"):
            self.assertEqual(
                prompts.resolve_report_language(language, "Summarize the results."),
                "fr",
            )
        self.assertEqual(
            prompts.resolve_report_language("en", "Résumez les résultats."), "en"
        )

    def test_header_uses_saved_language_for_missing_text_and_ocr_notices(self):
        config = {"report_language": "auto", "effective_report_language": "fr"}
        header = prompts.report_header(config, "study.pdf", 3, 1, [2], [3])
        for text in ("# Analyse de", "Pages PDF", "Texte insuffisant", "OCR utilisé"):
            self.assertIn(text, header)
        # Old auto snapshots keep their historical English rendering.
        self.assertTrue(
            prompts.report_header(
                {"report_language": "auto"}, "study.pdf", 3, 1, [], []
            ).startswith("# Analysis of")
        )


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
        self.store = JobStore(directory)
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.application = create_app(
            replace(app.SETTINGS, data_dir=directory), self.store
        )
        self.client = self.application.test_client()
        self.job_id = "0123456789abcdef" * 2
        with self.store.connect() as con:
            con.execute(
                """INSERT INTO jobs
                (id, filename, question, status, created_at, updated_at)
                VALUES (?, 'example.pdf', 'Analyze', 'done', ?, ?)""",
                (self.job_id, jobs.utcnow(), jobs.utcnow()),
            )
        self.directory = self.store.job_dir(self.job_id)
        self.directory.mkdir(parents=True)

    def test_markdown_attachment_headers_filename_and_utf8_content(self):
        """Verify headers and UTF-8 Markdown content."""
        content = "# Summary\n\nAnalysis completed — temperature: 25 °C ± 0.5 °C.\n"
        (self.directory / "rapport_final.md").write_text(content, encoding="utf-8")
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers.getlist("Content-Type"),
                ["text/markdown; charset=utf-8"],
            )
            self.assertEqual(
                response.headers["Content-Disposition"],
                "attachment; filename=example_rapport.md; filename*=UTF-8''example_rapport.md",
            )
            self.assertEqual(response.data, content.encode("utf-8"))

    def test_unfinished_jobs_cannot_download_a_report(self):
        """Reject unfinished jobs with HTTP 409."""
        for status in ("queued", "running", "failed"):
            with self.subTest(status=status):
                self.store.update_job(self.job_id, status=status)
                self.assertEqual(
                    self.client.get(f"/api/jobs/{self.job_id}/report").status_code, 409
                )

    def test_missing_job_or_report_returns_404(self):
        """Require HTTP 404 when the job or report is missing."""
        self.assertEqual(
            self.client.get(f"/api/jobs/{'f' * 32}/report").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/jobs/{self.job_id}/report").status_code, 404
        )

    def write_report(self, text):
        (self.directory / "rapport_final.md").write_text(text, encoding="utf-8")

    def test_preview_renders_structure_unicode_and_downloads_identical_bytes(self):
        text = (
            "# Étude des bâtiments\n\n## Données\n\nL’été à Grenoble.\nDeuxième ligne.\n\n"
            "- Élément\n\n> Citation\n\n| Lieu | Valeur |\n| --- | --- |\n| Paris | 25 °C |\n\n"
            "[Source](https://example.org)\n\n```python\nprint('é')\n```\n"
        )
        self.write_report(text)
        with patch.object(
            app, "ModelClient", side_effect=AssertionError("No model call")
        ):
            preview = self.client.get(f"/api/jobs/{self.job_id}/report/view?lang=fr")
            self.assertEqual(preview.status_code, 200)
            body = preview.get_data(as_text=True)
            for markup in (
                "<h1>Étude des bâtiments</h1>",
                "<h2>",
                "<ul>",
                "<blockquote>",
                "<table>",
                "<pre><code",
                "<br",
            ):
                self.assertIn(markup, body)
            self.assertIn("Télécharger", body)
            self.assertIn("Etude_des_batiments_rapport.md", body)
            self.assertIn(
                "default-src 'none'", preview.headers["Content-Security-Policy"]
            )
            self.assertEqual(preview.headers["Cache-Control"], "no-store")
            with self.client.get(f"/api/jobs/{self.job_id}/report") as download:
                self.assertEqual(download.data, text.encode("utf-8"))
        # Refreshing the preview requires no browser session or transient JavaScript state.
        self.assertEqual(
            self.client.get(f"/api/jobs/{self.job_id}/report/view").status_code, 200
        )
        self.assertEqual(
            self.store.get_job(self.job_id)["report_name"],
            "Etude_des_batiments_rapport.md",
        )
        self.assertEqual(
            list(self.directory.glob("*.md")), [self.directory / "rapport_final.md"]
        )

    def test_untrusted_html_images_and_executable_links_are_inert(self):
        self.write_report("""# Security report
<script>alert(1)</script><img src=x onerror=alert(1)>
<iframe src="https://example.org"></iframe>

[bad](javascript:alert%281%29) [bad](vbscript:msgbox) [bad](data:text/html,test)
[bad](file:///etc/passwd) [bad](custom:run)
![tracking](https://example.org/pixel.png)
[safe](https://example.org)
""")
        response = self.client.get(f"/api/jobs/{self.job_id}/report/view")
        tags = Tags(response.get_data(as_text=True)).tags
        self.assertFalse(
            any(tag in ("img", "iframe", "object", "embed") for tag, _ in tags)
        )
        scripts = [attrs for tag, attrs in tags if tag == "script"]
        self.assertEqual(scripts, [{"src": "/static/report.js"}])
        for _, attrs in tags:
            self.assertFalse(any(key.startswith("on") for key in attrs))
            self.assertFalse(
                attrs.get("href", "").startswith(
                    ("javascript:", "vbscript:", "data:", "file:", "custom:")
                )
            )

    def test_empty_and_oversized_previews_keep_original_download_available(self):
        self.write_report("")
        response = self.client.get(f"/api/jobs/{self.job_id}/report/view?lang=fr")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Aucun contenu disponible.", response.get_data(as_text=True))
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.data, b"")
        self.write_report("a" * (reports.PREVIEW_MAX_BYTES + 1))
        with patch.object(
            reports,
            "render_markdown",
            side_effect=AssertionError("Oversized preview parsed"),
        ):
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
        self.store.update_job(self.job_id, status="running")
        self.assertEqual(
            self.client.get(f"/api/jobs/{self.job_id}/report/view").status_code, 409
        )
        self.store.update_job(self.job_id, status="done")
        for path in (
            "not-an-id/report",
            "not-an-id/report/view",
            f"{self.job_id}/system_prompt.txt",
            f"{self.job_id}/report/../../source.pdf",
            f"{self.job_id}/report/artifacts/manifest.json",
        ):
            self.assertEqual(self.client.get("/api/jobs/" + path).status_code, 404)

    def test_symlinked_reports_and_job_roots_are_rejected(self):
        outside = Path(self.tmp.name) / "private.md"
        outside.write_text("SECRET", encoding="utf-8")
        (self.directory / "rapport_final.md").symlink_to(outside)
        for suffix in ("report", "report/view"):
            self.assertEqual(
                self.client.get(f"/api/jobs/{self.job_id}/{suffix}").status_code, 404
            )
        (self.directory / "rapport_final.md").unlink()
        self.directory.rmdir()
        self.directory.symlink_to(outside.parent, target_is_directory=True)
        self.assertEqual(
            self.client.get(f"/api/jobs/{self.job_id}/report").status_code, 404
        )

    def test_substitution_after_resolution_cannot_redirect_name_or_content(self):
        outside = Path(self.tmp.name) / "external.md"
        outside.write_text("# External secret\nPRIVATE_CONTENT", encoding="utf-8")
        path = self.directory / "rapport_final.md"
        original = web_reports.resolve_report
        for suffix in ("report", "report/view"):
            with self.subTest(suffix=suffix):
                self.write_report("# Trusted study\nORIGINAL_CONTENT")
                self.store.set_report_name(self.job_id, None)

                def substitute(identifier):
                    resolved = original(identifier)
                    path.unlink()
                    path.symlink_to(outside)
                    return resolved

                try:
                    with (
                        patch.object(
                            web_reports, "resolve_report", side_effect=substitute
                        ),
                        self.client.get(
                            f"/api/jobs/{self.job_id}/{suffix}"
                        ) as response,
                    ):
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(b"ORIGINAL_CONTENT", response.data)
                        self.assertNotIn(b"PRIVATE_CONTENT", response.data)
                        self.assertNotIn(b"External secret", response.data)
                        self.assertIn(
                            "Trusted_study_rapport.md",
                            response.headers.get("Content-Disposition", "")
                            if suffix == "report"
                            else response.get_data(as_text=True),
                        )
                finally:
                    path.unlink()

    def test_substitution_after_naming_keeps_download_and_view_on_opened_report(self):
        outside = Path(self.tmp.name) / "external.md"
        outside.write_text("# External secret\nPRIVATE_CONTENT", encoding="utf-8")
        path = self.directory / "rapport_final.md"
        original = web_reports.ensure_report_name
        for suffix in ("report", "report/view"):
            with self.subTest(suffix=suffix):
                self.write_report("# Trusted study\nORIGINAL_CONTENT")

                def substitute(job, opened):
                    name = original(job, opened)
                    path.unlink()
                    path.symlink_to(outside)
                    return name

                try:
                    with (
                        patch.object(
                            web_reports, "ensure_report_name", side_effect=substitute
                        ),
                        self.client.get(
                            f"/api/jobs/{self.job_id}/{suffix}"
                        ) as response,
                    ):
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(b"ORIGINAL_CONTENT", response.data)
                        self.assertNotIn(b"PRIVATE_CONTENT", response.data)
                finally:
                    path.unlink()

    def test_directory_and_file_substitution_before_open_are_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / self.job_id).mkdir()
        for directory in (outside, outside / self.job_id):
            (directory / "rapport_final.md").write_text("PRIVATE_CONTENT")
        path = self.directory / "rapport_final.md"
        self.write_report("# Trusted study")
        original = web_reports.job_directory
        for suffix in ("report", "report/view"):
            for target in (path, self.directory, self.directory.parent):
                with self.subTest(suffix=suffix, target=target):
                    retained = target.with_name(target.name + "-retained")

                    def substitute(identifier, target=target, retained=retained):
                        directory = original(identifier)
                        target.rename(retained)
                        target.symlink_to(
                            outside / path.name if target == path else outside,
                            target_is_directory=target != path,
                        )
                        return directory

                    try:
                        with (
                            patch.object(
                                web_reports, "job_directory", side_effect=substitute
                            ),
                            self.client.get(
                                f"/api/jobs/{self.job_id}/{suffix}"
                            ) as response,
                        ):
                            self.assertEqual(response.status_code, 404)
                            self.assertNotIn(b"PRIVATE_CONTENT", response.data)
                    finally:
                        target.unlink()
                        retained.rename(target)

    @contextmanager
    def opened_report_files(self):
        streams = []
        original = os.fdopen

        def observe(*args, **kwargs):
            stream = original(*args, **kwargs)
            streams.append(stream)
            return stream

        with patch.object(web_reports.os, "fdopen", side_effect=observe):
            yield streams

    def test_streamed_download_pins_file_until_response_closes(self):
        content = "# Trusted study\n" + "Original content — é\n" * 100000
        self.write_report(content)
        self.store.set_report_name(self.job_id, "Trusted_study_rapport.md")
        outside = Path(self.tmp.name) / "external.md"
        outside.write_text("PRIVATE_CONTENT")
        # X-Sendfile must not reintroduce a pathname open by an HTTP server.
        self.application.config["USE_X_SENDFILE"] = True
        with self.opened_report_files() as streams:
            response = self.client.get(
                f"/api/jobs/{self.job_id}/report", buffered=False
            )
            try:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.is_streamed)
                self.assertNotIn("X-Sendfile", response.headers)
                self.assertEqual(len(streams), 1)
                self.assertFalse(streams[0].closed)
                path = self.directory / "rapport_final.md"
                path.unlink()
                path.symlink_to(outside)
                self.assertEqual(response.data, content.encode("utf-8"))
                self.assertEqual(response.content_length, len(response.data))
            finally:
                response.close()
            self.assertTrue(streams[0].closed)

    def test_early_disconnect_closes_download_file(self):
        self.write_report("# Trusted study\n" + "x" * 100000)
        with self.opened_report_files() as streams:
            response = self.client.get(
                f"/api/jobs/{self.job_id}/report", buffered=False
            )
            self.assertFalse(streams[0].closed)
            response.close()  # Do not consume the rest of the body.
            self.assertTrue(streams[0].closed)

    def test_http_metadata_ranges_conditionals_and_descriptor_cleanup(self):
        content = b"# Trusted study\nORIGINAL_CONTENT"
        (self.directory / "rapport_final.md").write_bytes(content)
        url = f"/api/jobs/{self.job_id}/report"
        with self.client.get(url) as response:
            etag = response.headers["ETag"]
            modified = response.headers["Last-Modified"]
            self.assertEqual(response.content_length, len(content))
        cases = (
            ("HEAD", {}, 200, b""),
            ("GET", {"Range": "bytes=2-8"}, 206, content[2:9]),
            ("GET", {"If-None-Match": etag}, 304, b""),
            ("GET", {"If-Modified-Since": modified}, 304, b""),
            ("GET", {"Range": "bytes=99999-"}, 416, None),
        )
        for method, headers, status, expected in cases:
            with self.subTest(method=method, headers=headers):
                with (
                    self.opened_report_files() as streams,
                    self.client.open(url, method=method, headers=headers) as response,
                ):
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(
                        response.headers["X-Content-Type-Options"], "nosniff"
                    )
                    if expected is not None:
                        self.assertEqual(response.data, expected)
                    if status == 206:
                        self.assertEqual(
                            response.headers["Content-Range"],
                            f"bytes 2-8/{len(content)}",
                        )
                self.assertEqual(len(streams), 1)
                self.assertTrue(streams[0].closed)

    def test_invalid_utf8_view_is_404_but_original_bytes_remain_downloadable(self):
        content = b"# Broken \xff title\n\xff"
        (self.directory / "rapport_final.md").write_bytes(content)
        with self.opened_report_files() as streams:
            response = self.client.get(f"/api/jobs/{self.job_id}/report/view")
            self.assertEqual(response.status_code, 404)
            self.assertIn(
                "default-src 'none'", response.headers["Content-Security-Policy"]
            )
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertTrue(streams[0].closed)
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, content)

    def test_viewer_closes_file_on_success_and_size_limit(self):
        for content, status in (("# Valid", 200), ("x" * (1024 * 1024 + 1), 413)):
            with self.subTest(status=status):
                self.write_report(content)
                with self.opened_report_files() as streams:
                    response = self.client.get(f"/api/jobs/{self.job_id}/report/view")
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(len(streams), 1)
                    self.assertTrue(streams[0].closed)

    def test_io_errors_during_naming_and_response_creation_close_file(self):
        self.write_report("# Trusted study")
        for owner, operation, suffix in (
            (reports, "report_filename", "report"),
            (reports, "report_filename", "report/view"),
            (web_reports, "send_file", "report"),
        ):
            with self.subTest(operation=operation, suffix=suffix):
                self.store.set_report_name(self.job_id, None)
                with (
                    self.opened_report_files() as streams,
                    patch.object(owner, operation, side_effect=OSError("PRIVATE_PATH")),
                ):
                    response = self.client.get(f"/api/jobs/{self.job_id}/{suffix}")
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(b"PRIVATE_PATH", response.data)
                    self.assertEqual(len(streams), 1)
                    self.assertTrue(streams[0].closed)

    def test_missing_special_and_hardlinked_reports_fail_safely(self):
        path = self.directory / "rapport_final.md"
        outside = Path(self.tmp.name) / "external.md"
        outside.write_text("PRIVATE_CONTENT")
        for create in (
            lambda: None,
            lambda: os.mkfifo(path),
            path.mkdir,
            lambda: os.link(outside, path),
        ):
            create()
            try:
                for suffix in ("report", "report/view"):
                    with self.client.get(
                        f"/api/jobs/{self.job_id}/{suffix}"
                    ) as response:
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn(b"PRIVATE_CONTENT", response.data)
            finally:
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)

    def test_open_failure_closes_directory_and_file_descriptors(self):
        self.write_report("# Trusted study")
        opened = []
        original = os.open

        def observe(*args, **kwargs):
            descriptor = original(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with (
            patch.object(web_reports.os, "open", side_effect=observe),
            patch.object(web_reports.os, "fdopen", side_effect=OSError("PRIVATE_PATH")),
        ):
            response = self.client.get(f"/api/jobs/{self.job_id}/report")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(opened), 3)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

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
        with (
            patch.object(
                Path, "read_text", side_effect=AssertionError("List read a report")
            ),
            patch.object(
                reports,
                "report_filename",
                side_effect=AssertionError("List parsed Markdown"),
            ),
        ):
            jobs = self.client.get("/api/jobs").json["jobs"]
        self.assertTrue(jobs[0]["report_available"])
        self.assertIsNone(jobs[0]["report_name"])
        with self.client.get(f"/api/jobs/{self.job_id}/report") as response:
            self.assertIn(
                "Etude_geotechnique_du_site_a_rapport.md",
                response.headers["Content-Disposition"],
            )
        with (
            patch.object(
                reports,
                "report_filename",
                side_effect=AssertionError("Name computed twice"),
            ),
            self.client.get(f"/api/jobs/{self.job_id}/report") as response,
        ):
            self.assertEqual(response.status_code, 200)
        (self.directory / "rapport_final.md").unlink()
        self.assertFalse(
            self.client.get("/api/jobs").json["jobs"][0]["report_available"]
        )


class NamingTests(unittest.TestCase):
    def test_descriptive_heading_and_generic_prefixes(self):
        cases = [
            (
                "# Analyse du Document : Consommation Énergétique des Bâtiments (2025–2026)",
                "Consommation_energetique_des_batiments_2025-2026_rapport.md",
            ),
            (
                "# Analyse de fichier.pdf\n## Rapport final\n### Consommation énergétique des bâtiments",
                "Consommation_energetique_des_batiments_rapport.md",
            ),
            (
                "# Final report: **Geothermal** `measurements`",
                "Geothermal_measurements_rapport.md",
            ),
            ("Étude des sols\n==============", "Etude_des_sols_rapport.md"),
        ]
        for markdown, expected in cases:
            with self.subTest(markdown=markdown):
                self.assertEqual(
                    reports.report_filename(markdown, "input.pdf"), expected
                )

    def test_fallback_portability_length_and_malformed_titles(self):
        generic = "# Analyse du document\n## Rapport final\n## Summary\n## Introduction\n## 12345\n"
        for markdown in ("", generic, "```md\n# Fake heading\n```", "# 😀"):
            self.assertEqual(
                reports.report_filename(markdown, "Etude_geotechnique_site_A.pdf"),
                "Etude_geotechnique_site_A_rapport.md",
            )
        for title in (
            "# ",
            '# <>:"/\\|?*\x00',
            "# CON",
            "# " + "Éléphants " * 300,
            "# 中文",
        ):
            name = reports.report_filename(title, "../étrange\x00:?*\\AUX.pdf")
            self.assertLessEqual(len(name), 100)
            self.assertRegex(name, r"^[A-Za-z0-9_-]+_rapport\.md$")
        self.assertEqual(reports.report_filename("", "???"), "document_rapport.md")


if __name__ == "__main__":
    unittest.main()
