"""Keep public failure codes typed, translated and visible to the browser."""

import json
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import pymupdf
import requests

import app
from document_processor import extraction, jobs
from document_processor.config import ConfigurationError
from document_processor.errors import Cancelled, ModelError, ProcessingError
from document_processor.jobs import JobStore
from document_processor.worker import Worker, record_failure

ROOT = Path(app.__file__).parent


class ErrorHierarchyTests(unittest.TestCase):
    def test_cancellation_is_a_public_processing_signal_separate_from_backend_errors(
        self,
    ):
        cancelled = Cancelled()
        self.assertIsInstance(cancelled, ProcessingError)
        self.assertIsInstance(cancelled, RuntimeError)
        self.assertNotIsInstance(cancelled, ModelError)
        self.assertEqual(cancelled.args, ("Analysis cancelled.",))
        self.assertEqual(
            json.loads(
                json.dumps({"error_code": cancelled.code, "error": str(cancelled)})
            ),
            {"error_code": "cancelled", "error": "Analysis cancelled."},
        )
        self.assertIsInstance(
            ModelError("backend_unavailable", "Unavailable."), ProcessingError
        )
        self.assertIsInstance(
            ConfigurationError("invalid_options", "Invalid."), ValueError
        )


class ExtractionErrorTests(unittest.TestCase):
    """Extraction failures carry stable codes instead of generic exceptions."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {**app.runtime_defaults()["extraction"], "enabled": False}

    def pdf(
        self,
        name,
        pages,
        text="Readable measurements and conclusions for the extraction test. " * 3,
        **save,
    ):
        path = self.root / name
        with pymupdf.open() as document:
            for _ in range(pages):
                page = document.new_page()
                if text:
                    page.insert_textbox(pymupdf.Rect(36, 36, 560, 800), text)
            document.save(path, **save)
        return path

    def assertCode(self, code, path, **settings):
        with self.assertRaises(ProcessingError) as raised:
            extraction.extract_pdf(path, {**self.settings, **settings})
        self.assertEqual(raised.exception.code, code)

    def test_unreadable_pdf(self):
        path = self.root / "broken.pdf"
        path.write_bytes(b"%PDF-not really a document")
        self.assertCode("unreadable_pdf", path)

    def test_encrypted_pdf(self):
        path = self.pdf(
            "locked.pdf",
            1,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="user",
        )
        self.assertCode("encrypted_pdf", path)

    def test_page_limit(self):
        self.assertCode("page_limit", self.pdf("long.pdf", 2), max_pages=1)

    def test_no_usable_text_without_ocr(self):
        self.assertCode("no_usable_text", self.pdf("blank.pdf", 1, text=""))


class FailedJobTests(unittest.TestCase):
    """The worker exposes public failures and hides unexpected exception text."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        patcher = patch.object(app, "store", JobStore(root))
        patcher.start()
        self.addCleanup(patcher.stop)
        app.store.initialize()

    def job(self):
        identifier = uuid.uuid4().hex
        with app.store.connect() as con:
            con.execute(
                """INSERT INTO jobs (id, filename, question, status, stage, created_at, updated_at)
                VALUES (?, 'doc.pdf', 'Summarize', 'running', 'Running', ?, ?)""",
                (identifier, jobs.utcnow(), jobs.utcnow()),
            )
        return identifier

    def test_public_failures_keep_their_code_and_message(self):
        for exc in (
            ProcessingError("encrypted_pdf", "Encrypted PDF."),
            ModelError("deadline", "Too slow."),
            ConfigurationError("context_insufficient", "Too large."),
        ):
            with self.subTest(code=exc.code):
                identifier = self.job()
                record_failure(app.store, identifier, exc)
                job = app.store.get_job(identifier)
                self.assertEqual(
                    (job["status"], job["error_code"], job["error"]),
                    ("failed", exc.code, str(exc)),
                )

    def test_unexpected_failures_never_expose_private_text(self):
        identifier = self.job()
        record_failure(app.store, identifier, OSError("/private/path/secret.pdf"))
        job = app.store.get_job(identifier)
        self.assertEqual(job["error_code"], "analysis_failed")
        self.assertNotIn("secret", job["error"])

    def test_backend_request_failure_remains_a_failed_job_with_a_safe_public_code(self):
        identifier = self.job()
        client = app.BackendClient("http://localhost", "", app.runtime_defaults(), "")
        self.addCleanup(client.session.close)
        runner = Worker(app.store, lambda job_id, check: client.model())
        with (
            patch.object(
                client.session,
                "request",
                side_effect=requests.ConnectionError("PRIVATE_REMOTE_BODY"),
            ),
            self.assertLogs(app.log, level="ERROR") as logs,
        ):
            runner.run_claimed(identifier)
        response = app.app.test_client().get(f"/api/jobs/{identifier}")
        self.assertEqual(response.status_code, 200)
        job = response.get_json()
        self.assertEqual(
            (job["status"], job["error_code"]), ("failed", "backend_unavailable")
        )
        self.assertEqual(
            job["error"], "The model backend is unavailable or returned invalid data."
        )
        self.assertNotIn(
            "PRIVATE_REMOTE_BODY",
            response.get_data(as_text=True) + " ".join(logs.output),
        )


class ErrorCatalogTests(unittest.TestCase):
    """Every public error code is translated, and every translated code is still emitted."""

    def test_emitted_codes_and_translations_match(self):
        sources = [ROOT / "app.py", *sorted((ROOT / "document_processor").glob("*.py"))]
        text = {path: path.read_text(encoding="utf-8") for path in sources}
        emitted = set()
        for _path, source in text.items():
            emitted |= set(
                re.findall(
                    r"(?:ProcessingError|ModelError|ConfigurationError)\(\s*\"([a-z_]+)\"",
                    source,
                )
            )
            emitted |= set(
                re.findall(r"jsonify\([^\n]*?error_code=\"([a-z_]+)\"", source)
            )
        # Route helpers in app.py also return API dictionaries with explicit codes,
        # and api_error reports each registered HTTP status as http_<status>.
        emitted |= set(
            re.findall(r"\"error_code\": \"([a-z_]+)\"", text[ROOT / "app.py"])
        )
        statuses = set(
            re.findall(r"@app\.errorhandler\((\d{3})\)", text[ROOT / "app.py"])
        )
        emitted |= {f"http_{status}" for status in statuses}
        catalog = json.loads(
            (ROOT / "static/translations.json").read_text(encoding="utf-8")
        )
        for language in ("en", "fr"):
            translated = {
                key[len("error.") :]
                for key in catalog[language]
                if key.startswith("error.")
            }
            with self.subTest(language=language):
                self.assertEqual(
                    emitted - translated, set(), "untranslated error codes"
                )
                stale = {
                    code
                    for code in translated - emitted - {"generic"}
                    if not any(f'"{code}"' in source for source in text.values())
                }
                self.assertEqual(
                    stale, set(), "translated error codes that are never emitted"
                )


class CapabilityCacheTests(unittest.TestCase):
    """A job's backend inspection also informs the next form validation."""

    def setUp(self):
        for name, value in (
            ("_capability_cache", {"value": None, "expires": 0}),
            ("read_system_prompt", lambda: "Private instructions"),
        ):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def warnings(self):
        response = self.client.post("/api/config/preview", json={"profile": "low"})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["configuration"]["warnings"]

    def test_inspection_propagates_cancellation_at_both_requests_without_caching_a_fallback(
        self,
    ):
        # Keep callback signatures; these fixtures are consumed within each iteration.
        for path in ("/v1/models", "/props"):
            with self.subTest(path=path):
                client = app.ModelClient()
                self.addCleanup(client.session.close)
                previous = client.capabilities
                cached = dict(app._capability_cache)
                client.cancel_check = lambda: path == "/v1/models"  # noqa: B023

                def model_response(*args, **kwargs):
                    client.cancel_check = lambda: True  # noqa: B023
                    response = requests.Response()
                    response.status_code = 200
                    response._content = b'{"data": [{"id": "test-model"}]}'
                    response._content_consumed = True
                    return response

                with (
                    patch.object(
                        client.session, "request", side_effect=model_response
                    ) as request,
                    self.assertRaises(Cancelled) as raised,
                ):
                    client.inspect()
                self.assertEqual(raised.exception.code, "cancelled")
                self.assertEqual(request.call_count, 0 if path == "/v1/models" else 1)
                self.assertIs(client.capabilities, previous)
                self.assertEqual(app._capability_cache, cached)

    def test_backend_failures_still_return_sanitized_inspection_fallbacks(self):
        for path in ("/v1/models", "/props"):
            with (
                self.subTest(path=path),
                patch.object(app, "_capability_cache", {"value": None, "expires": 0}),
            ):
                client = app.ModelClient()
                self.addCleanup(client.session.close)
                with (
                    patch.object(client, "model", wraps=client.model) as model,
                    patch.object(
                        client.session,
                        "request",
                        side_effect=requests.ConnectionError("PRIVATE_REMOTE_BODY"),
                    ),
                ):
                    if path == "/props":
                        model.return_value = "test-model"
                    result = client.inspect()
                self.assertEqual(
                    result["status"],
                    "unavailable" if path == "/v1/models" else "available",
                )
                if path == "/props":
                    self.assertEqual(result["properties"], "unsupported_or_unavailable")
                self.assertEqual(app.cached_capabilities(), result)
                self.assertNotIn("PRIVATE_REMOTE_BODY", json.dumps(result))

    def test_validation_errors_keep_http_400_and_safe_codes_and_messages(self):
        cases = [
            (
                {"profile": "PRIVATE_INVALID_VALUE"},
                "invalid_profile",
                "Unknown performance profile.",
            ),
            (
                {"overrides": {"analysis_tokens": -1}},
                "invalid_budget",
                "Invalid analysis_tokens budget.",
            ),
        ]
        with patch(
            "requests.Session.request", side_effect=AssertionError("No backend request")
        ):
            for options, code, message in cases:
                with self.subTest(code=code):
                    preview = self.client.post("/api/config/preview", json=options)
                    upload = self.client.post(
                        "/api/jobs", data={"options": json.dumps(options)}
                    )
                    for response in (preview, upload):
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(
                            response.get_json(), {"error_code": code, "error": message}
                        )

    def test_job_inspection_is_shared_with_configuration_preview(self):
        self.assertIn("engine_context_unknown", self.warnings())
        observed = {"status": "available", "context_tokens": 8192, "model": "test"}
        with patch.object(app.BackendClient, "inspect", return_value=observed):
            client = app.ModelClient()
            self.addCleanup(client.session.close)
            client.inspect()
        self.assertEqual(app.cached_capabilities(), observed)
        self.assertNotIn("engine_context_unknown", self.warnings())


if __name__ == "__main__":
    unittest.main()
