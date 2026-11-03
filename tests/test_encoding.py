"""Locate Unicode regressions at extraction, HTTP, channel and artifact boundaries."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.utils import get_encoding_from_headers

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pdf-encoding-test-"))
os.environ.setdefault("SYSTEM_PROMPT_FILE", str(Path(__file__).resolve().parents[1] / "config/system_prompt.example.txt"))

import app
from document_processor import config, jobs, prompts
from document_processor.errors import ProcessingError
from document_processor.model_client import ModelError


FRENCH = "L’assemblée générale a décidé de vérifier la présence des élèves à Grenoble. Résumé : température −2,5 °C, échantillon n° 3, précision ±0,2 %."
ENGLISH = "The general assembly verified the students’ attendance. Temperature: −2.5 °C; sample #3; precision ±0.2 %."
MULTILINGUAL = FRENCH + " English: œ æ «quotes» — α β µm ∑ ≤ ≥ → 中文 日本語 العربية 😀 e\u0301. Valid letters: Ã Â â; separators: \u0085 \u2028 \u2029."


class FragmentedBytes(io.BytesIO):
    """Make real Requests iteration see arbitrary network boundaries."""

    def __init__(self, content, fragment_size):
        super().__init__(content)
        self.fragment_size = fragment_size

    def read(self, size=-1):
        return super().read(min(size, self.fragment_size) if size >= 0 else self.fragment_size)


def response_for(body, content_type, fragment_size=1):
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = content_type
    response.encoding = get_encoding_from_headers(response.headers)
    response.raw = FragmentedBytes(body, fragment_size)
    return response


def sse_body(text, channel="separated"):
    def event(delta):
        return {"choices": [{"delta": delta, "finish_reason": None}]}

    if channel == "separated":
        events = [event({"reasoning_content": text}), event({"content": text})]
    else:
        events = [event({"content": part}) for part in ("<thi", "nk>" + text, "</thi", "nk>" + text)]
    events.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    # ensure_ascii=False is essential: escaped ASCII JSON hid the original bug.
    return ("".join("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n"
                    for value in events) + "data: [DONE]\r\n\r\n").encode("utf-8")


class ModelAdapter(BaseAdapter):
    """Exercise Requests serialization and decoding without any network or model."""

    def __init__(self, text):
        super().__init__()
        self.text = text
        self.completions = []
        self.fail_final = False

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        payload = json.loads(request.body.decode("utf-8")) if request.body else None
        if path.endswith("/chat/completions"):
            self.completions.append(payload)
            instruction = payload["messages"][-1]["content"]
            if self.fail_final and instruction.startswith(prompts.TEMPLATES["final"]):
                body = b'data: {"error": "simulated failure"}\n\n'
            else:
                body = sse_body(self.text)
            return response_for(body, "text/event-stream")
        if path.endswith("/models"):
            result = {"data": [{"id": "unicode-test"}]}
        elif path.endswith("/props"):
            result = {"default_generation_settings": {"n_ctx": 16384}, "chat_template": "é«template»"}
        elif path.endswith("/tokenize"):
            result = {"tokens": list(range(len(payload["content"].split())))}
        else:
            raise AssertionError("Unexpected model endpoint")
        return response_for(json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json")

    def close(self):
        pass


class EncodingBoundaryTests(unittest.TestCase):
    def client(self):
        with patch.object(app, "read_system_prompt", return_value=MULTILINGUAL):
            client = app.ModelClient()
        self.addCleanup(client.session.close)
        return client

    def test_system_prompt_file_is_utf8_and_invalid_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.txt"
            path.write_bytes(MULTILINGUAL.encode("utf-8"))
            with patch.object(app, "SYSTEM_FILE", path):
                self.assertEqual(app.read_system_prompt(), MULTILINGUAL)
                path.write_bytes(b"\xff")
                with self.assertRaises(config.ConfigurationError):
                    app.read_system_prompt()

    def test_simulated_pdf_readers_preserve_exact_unicode(self):
        for reader in ("pymupdf", "pypdf"):
            for text in (FRENCH, ENGLISH, MULTILINGUAL):
                with self.subTest(reader=reader, text=text):
                    document = MagicMock()
                    document.__enter__.return_value = document
                    document.needs_pass = False
                    document.__len__.return_value = 1
                    document.__getitem__.return_value.get_text.return_value = text if reader == "pymupdf" else ""
                    fallback = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
                    with patch.object(app.pymupdf, "open", return_value=document), \
                         patch.object(app, "PdfReader", return_value=fallback) as secondary:
                        pages, count, missing, ocr = app.extract_pdf("unused.pdf")
                    self.assertEqual(pages, [{"page": 1, "text": text}], "PDF reader / normalization")
                    self.assertEqual((count, missing, ocr), (1, [], []))
                    self.assertEqual(secondary.call_count, int(reader == "pypdf"))

    def test_ocr_decodes_utf8_once_and_rejects_invalid_bytes(self):
        page = MagicMock()
        page.rect.width = page.rect.height = 72
        page.get_pixmap.return_value.tobytes.return_value = b"PNG"
        for text in (FRENCH, ENGLISH, MULTILINGUAL):
            with self.subTest(text=text), patch.object(app.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=text.encode("utf-8"))):
                self.assertEqual(app.ocr_page(page, 1), text, "OCR byte boundary")
        with patch.object(app.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"\xff")):
            with self.assertRaises(ProcessingError) as error:
                app.ocr_page(page, 1)
        self.assertEqual(error.exception.code, "invalid_ocr_encoding")

    def test_request_json_and_prompt_preserve_unicode(self):
        client = self.client()
        adapter = ModelAdapter(MULTILINGUAL)
        client.session.mount("http://", adapter)
        client.session.mount("https://", adapter)
        self.assertEqual(client.complete(FRENCH, MULTILINGUAL, 100), MULTILINGUAL)
        messages = adapter.completions[0]["messages"]
        self.assertEqual(messages[0]["content"], MULTILINGUAL, "system prompt / outgoing JSON")
        self.assertEqual(messages[1]["content"], FRENCH + "\n\n" + MULTILINGUAL, "user JSON")

    def test_json_response_uses_utf8_despite_misleading_http_charset(self):
        client = self.client()
        for content_type in ("application/json", "text/plain", "application/json; charset=iso-8859-1"):
            with self.subTest(content_type=content_type):
                response = response_for(json.dumps({"text": MULTILINGUAL}, ensure_ascii=False).encode("utf-8"), content_type)
                with patch.object(client.session, "request", return_value=response):
                    self.assertEqual(client.json_request("GET", "/test"), {"text": MULTILINGUAL})
        with patch.object(client.session, "request", return_value=response_for(b'{"text":"\xff"}', "application/json")):
            with self.assertRaises(ModelError) as error:
                client.json_request("GET", "/test")
        self.assertEqual(error.exception.code, "backend_unavailable")

    def test_sse_utf8_split_network_bytes_and_both_reasoning_channels(self):
        for text in (FRENCH, ENGLISH, MULTILINGUAL):
            for channel in ("separated", "tags"):
                for size in (1, 2, 3, 7, 64):
                    for content_type in ("text/event-stream", "text/event-stream; charset=utf-8",
                                         "text/event-stream; charset=windows-1252"):
                        with self.subTest(text=text, channel=channel, size=size, content_type=content_type):
                            client = self.client()
                            preview = []
                            response = response_for(sse_body(text, channel), content_type, size)
                            with patch.object(client, "tokenize", return_value=10), \
                                 patch.object(client, "model", return_value="test"), \
                                 patch.object(client.session, "post", return_value=response):
                                result = client.generate("Analyze", text, 100,
                                    on_update=lambda *args: preview.append(args))
                            self.assertEqual(result.content, text, "SSE / answer channel")
                            self.assertEqual(preview[-1], (text, text, True), "reasoning / live preview")

    def test_invalid_sse_utf8_cannot_become_a_completed_artifact(self):
        client = self.client()
        response = response_for(b'data: {"choices":[{"delta":{"content":"\xff"}}]}\n\n', "text/event-stream")
        with patch.object(client, "tokenize", return_value=10), \
             patch.object(client, "model", return_value="test"), \
             patch.object(client.session, "post", return_value=response):
            with self.assertRaises(ModelError) as error:
                client.complete("Analyze", "Source", 100)
        self.assertEqual(error.exception.code, "invalid_stream_encoding")
        self.assertIsNone(client.last_result)


class EncodingPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (("DATA", self.root), ("DB_PATH", self.root / "jobs.sqlite3"),
                            ("LIVE_OUTPUT_ENABLED", True)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.init_db()
        self.http = app.app.test_client()

    def create(self, text, language):
        with patch.object(app, "read_system_prompt", return_value=text):
            response = self.http.post("/api/jobs", data={
                "file": (io.BytesIO(b"%PDF-test"), "Élèves — étude.pdf"), "question": text,
                "options": json.dumps({"report_language": language, "overrides": {"chunk_tokens": 128}})})
        self.assertEqual(response.status_code, 201)
        return response.json["id"]

    def run_job(self, identifier, text, adapter):
        original_client = app.ModelClient

        def model(config):
            client = original_client(config)
            client.session.mount("http://", adapter)
            client.session.mount("https://", adapter)
            return client

        # Real chunking, tokenizer HTTP, prompts, generation, files and SQLite.
        pages = [{"page": i + 1, "text": text + "\n\n" + "padding " * 70} for i in range(5)]
        app.update_job(identifier, status="running")
        with patch.object(app, "extract_pdf", return_value=(pages, 5, [], [])), \
             patch.object(app, "ModelClient", side_effect=model):
            app.analyze_job(identifier)

    def test_unicode_extraction_to_merge_report_live_download_and_retry(self):
        for text, language in ((FRENCH, "fr"), (ENGLISH, "en"), (MULTILINGUAL, "fr")):
            with self.subTest(language=language, text=text):
                identifier = self.create(text, language)
                adapter = ModelAdapter(text)
                adapter.fail_final = True
                with self.assertRaises(ModelError):
                    self.run_job(identifier, text, adapter)
                app.update_job(identifier, status="failed")
                directory = app.job_dir(identifier)
                root = next(directory.glob("artifacts/*"))
                extracted = json.loads((root / "extracted.json").read_text(encoding="utf-8"))
                self.assertEqual(extracted["pages"][0]["text"].split("\n\n")[0], text, "extraction JSON")
                chunks = json.loads((root / "chunks.json").read_text(encoding="utf-8"))
                self.assertEqual(len(chunks), 5, "real chunking")
                self.assertTrue(all(text in chunk["text"] for chunk in chunks), "chunk Unicode")
                for path in (*root.glob("analysis_*.md"), *root.glob("merge_*.md")):
                    self.assertEqual(path.read_bytes(), text.encode("utf-8"), path.name)
                self.assertEqual(len(list(root.glob("analysis_*.md"))), 5)
                self.assertEqual(len(list(root.glob("merge_*.md"))), 1)
                merge = next(payload for payload in adapter.completions
                             if payload["messages"][-1]["content"].startswith(prompts.TEMPLATES["merge"]))
                self.assertEqual(merge["messages"][-1]["content"].count(text), 5, "4 merged notes + question")
                count = len(adapter.completions)
                self.assertEqual(self.http.post(f"/api/jobs/{identifier}/retry").status_code, 200)
                adapter.fail_final = False
                self.run_job(identifier, text, adapter)
                self.assertEqual(len(adapter.completions), count + 1, "retry must reuse Unicode notes and merges")
                final_input = adapter.completions[-1]["messages"][-1]["content"]
                self.assertEqual(final_input.count(text), 3, "merged note + singleton + question")
                saved = jobs.load_config(app.db_conn, identifier)
                self.assertEqual(saved["system_prompt"], text, "SQLite snapshot")
                expected = prompts.report_header(saved, "Élèves — étude.pdf", 5, 5, [], []) + text + "\n"
                self.assertEqual((root / "rapport_final.md").read_text(encoding="utf-8"), text, "final artifact")
                self.assertEqual((directory / "rapport_final.md").read_bytes(), expected.encode("utf-8"), "published report")
                for route in (f"/api/jobs/{identifier}", "/api/jobs"):
                    payload = self.http.get(route).json
                    job = next(item for item in payload["jobs"] if item["id"] == identifier) if "jobs" in payload else payload
                    self.assertEqual(job["question"], text, "SQLite / JSON API")
                live = self.http.get(f"/api/jobs/{identifier}/live?include_reasoning=true").json
                self.assertEqual((live["content"], live["reasoning"]), (text, text), "preview disk / JSON API")
                with self.http.get(f"/api/jobs/{identifier}/report") as download:
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.headers["Content-Type"], "text/markdown; charset=utf-8")
                    self.assertEqual(download.data.decode("utf-8"), expected, "HTTP download")
                metrics = self.http.get(f"/api/jobs/{identifier}/metrics").json
                self.assertEqual(metrics["summary"]["reused_steps"], 8, "extraction, chunks, sections and merge")

    def test_pre_fix_transport_artifacts_are_recomputed_on_retry_and_report_is_archived(self):
        identifier = self.create(FRENCH, "fr")
        adapter = ModelAdapter(FRENCH.encode("utf-8").decode("latin-1"))

        def old_fingerprint(value):
            if isinstance(value, dict) and "text_transport_version" in value:
                value = {key: item for key, item in value.items() if key != "text_transport_version"}
            return config.fingerprint(value)

        with patch("document_processor.pipeline.fingerprint", side_effect=old_fingerprint), \
             patch("document_processor.jobs.fingerprint", side_effect=old_fingerprint):
            self.run_job(identifier, FRENCH, adapter)
        report = app.job_dir(identifier) / "rapport_final.md"
        original = report.read_bytes()
        count = len(adapter.completions)
        app.update_job(identifier, status="failed")
        self.assertEqual(self.http.post(f"/api/jobs/{identifier}/retry").status_code, 200)
        self.assertEqual(report.read_bytes(), original, "queuing a retry must leave the old report untouched")
        adapter.text = FRENCH
        self.run_job(identifier, FRENCH, adapter)
        self.assertEqual(len(adapter.completions), count * 2, "pre-fix generated artifacts must not be reused")
        archives = list(report.parent.glob("previous_reports/*.md"))
        self.assertTrue(any(path.read_bytes() == original for path in archives), "old report backup")
        self.assertIn(FRENCH, report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
