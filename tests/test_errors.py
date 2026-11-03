"""Keep public failure codes typed, translated and visible to the browser."""

import json
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app
from document_processor import extraction
from document_processor.config import ConfigurationError
from document_processor.errors import ProcessingError
from document_processor.model_client import ModelError

ROOT = Path(app.__file__).parent


class ExtractionErrorTests(unittest.TestCase):
    """Extraction failures carry stable codes instead of generic exceptions."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {**app.runtime_defaults()["extraction"], "enabled": False}

    def pdf(self, name, pages, text="Readable measurements and conclusions for the extraction test. " * 3, **save):
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
        path = self.pdf("locked.pdf", 1, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                        owner_pw="owner", user_pw="user")
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
        for name, value in (("DATA", root), ("DB_PATH", root / "jobs.sqlite3")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.init_db()

    def job(self):
        identifier = uuid.uuid4().hex
        with app.db_conn() as con:
            con.execute("""INSERT INTO jobs (id, filename, question, status, stage, created_at, updated_at)
                VALUES (?, 'doc.pdf', 'Summarize', 'running', 'Running', ?, ?)""", (identifier, app.utcnow(), app.utcnow()))
        return identifier

    def test_public_failures_keep_their_code_and_message(self):
        for exc in (ProcessingError("encrypted_pdf", "Encrypted PDF."), ModelError("deadline", "Too slow."),
                    ConfigurationError("context_insufficient", "Too large.")):
            with self.subTest(code=exc.code):
                identifier = self.job()
                app.fail_job(identifier, exc)
                job = app.get_job(identifier)
                self.assertEqual((job["status"], job["error_code"], job["error"]), ("failed", exc.code, str(exc)))

    def test_unexpected_failures_never_expose_private_text(self):
        identifier = self.job()
        app.fail_job(identifier, OSError("/private/path/secret.pdf"))
        job = app.get_job(identifier)
        self.assertEqual(job["error_code"], "analysis_failed")
        self.assertNotIn("secret", job["error"])


class ErrorCatalogTests(unittest.TestCase):
    """Every public error code is translated, and every translated code is still emitted."""

    def test_emitted_codes_and_translations_match(self):
        sources = [ROOT / "app.py", *sorted((ROOT / "document_processor").glob("*.py"))]
        text = {path: path.read_text(encoding="utf-8") for path in sources}
        emitted = set()
        for path, source in text.items():
            emitted |= set(re.findall(r"(?:ProcessingError|ModelError|ConfigurationError)\(\s*\"([a-z_]+)\"", source))
            emitted |= set(re.findall(r"jsonify\([^\n]*?error_code=\"([a-z_]+)\"", source))
        # Route helpers in app.py also return API dictionaries with explicit codes,
        # and api_error reports each registered HTTP status as http_<status>.
        emitted |= set(re.findall(r"\"error_code\": \"([a-z_]+)\"", text[ROOT / "app.py"]))
        statuses = set(re.findall(r"@app\.errorhandler\((\d{3})\)", text[ROOT / "app.py"]))
        emitted |= {f"http_{status}" for status in statuses}
        catalog = json.loads((ROOT / "static/translations.json").read_text(encoding="utf-8"))
        for language in ("en", "fr"):
            translated = {key[len("error."):] for key in catalog[language] if key.startswith("error.")}
            with self.subTest(language=language):
                self.assertEqual(emitted - translated, set(), "untranslated error codes")
                stale = {code for code in translated - emitted - {"generic"}
                         if not any(f'"{code}"' in source for source in text.values())}
                self.assertEqual(stale, set(), "translated error codes that are never emitted")


class CapabilityCacheTests(unittest.TestCase):
    """A job's backend inspection also informs the next form validation."""

    def setUp(self):
        for name, value in (("_capability_cache", {"value": None, "expires": 0}),
                            ("read_system_prompt", lambda: "Private instructions")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def warnings(self):
        response = self.client.post("/api/config/preview", json={"profile": "low"})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["configuration"]["warnings"]

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
