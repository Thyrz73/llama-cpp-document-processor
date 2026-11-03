"""Prompt history and bounded operational logs without a running model."""

import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import jobs, observability
from document_processor.jobs import JobStore


class PromptLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in (("store", JobStore(root)),
                            ("read_system_prompt", lambda: "PRIVATE_SERVER_PROMPT")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app.store.initialize()
        self.client = app.app.test_client()

    def create(self, question):
        response = self.client.post("/api/jobs", data={
            "file": (io.BytesIO(b"%PDF-test"), "study.pdf"), "question": question})
        self.assertEqual(response.status_code, 201)
        return response.json["id"]

    def test_custom_prompt_preserves_full_text_in_active_and_completed_history(self):
        prompt = "  Instructions privées : électricité.\n\n<script>alert('test')</script>\nSuite.  "
        identifier = self.create(prompt)
        for status in ("queued", "running", "done"):
            app.store.update_job(identifier, status=status)
            with patch.object(app, "read_system_prompt", return_value="CHANGED_SYSTEM_PROMPT"):
                detail = self.client.get(f"/api/jobs/{identifier}").json
                listed = self.client.get("/api/jobs").json["jobs"][0]
            self.assertEqual(detail["question"], prompt)
            self.assertEqual(detail["question_custom"], 1)
            self.assertEqual(listed["question"], prompt)
            self.assertNotIn("PRIVATE_SERVER_PROMPT", json.dumps(detail))
            self.assertNotIn("CHANGED_SYSTEM_PROMPT", json.dumps(detail))

    def test_default_prompts_are_distinguished_from_custom_instructions(self):
        for prompt in app.DEFAULT_QUESTIONS:
            identifier = self.create(prompt)
            self.assertEqual(app.store.get_job(identifier)["question_custom"], 0)

    def test_old_schema_migrates_idempotently_without_inventing_prompt_origin(self):
        legacy = JobStore(app.store.data_dir / "legacy")
        legacy.data_dir.mkdir()
        with sqlite3.connect(legacy.db_path) as con:
            con.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', pages INTEGER DEFAULT 0,
                total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
                error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            con.execute("INSERT INTO jobs (id,filename,question,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                        ("f" * 32, "old.pdf", "Saved legacy request", "done", jobs.utcnow(), jobs.utcnow()))
            con.execute("INSERT INTO jobs (id,filename,question,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                        ("e" * 32, "old.pdf", app.FALLBACK_QUESTION, "done", jobs.utcnow(), jobs.utcnow()))
        with patch.object(app, "store", legacy):
            legacy.initialize()
            legacy.initialize()
            job = self.client.get("/api/jobs/" + "f" * 32).json
            listed = {item["id"]: item for item in self.client.get("/api/jobs").json["jobs"]}
        self.assertEqual(job["question"], "Saved legacy request")
        self.assertIsNone(job["question_custom"])
        self.assertEqual(job["progress_percent"], 100)
        # The server, not the browser, recognizes an old request that is a default instruction.
        self.assertIsNone(listed["f" * 32]["question_custom"])
        self.assertEqual(listed["e" * 32]["question_custom"], 0)

    def test_creation_download_preview_and_cancellation_logs_contain_no_prompt(self):
        secret = "NEVER_LOG_THIS_REQUEST"
        with self.assertLogs(app.log, "INFO") as captured:
            identifier = self.create(secret)
            self.client.post(f"/api/jobs/{identifier}/cancel")
            with app.store.connect() as con:
                con.execute("UPDATE jobs SET cancel_requested=0 WHERE id=?", (identifier,))
            app.store.update_job(identifier, status="done")
            (app.store.job_dir(identifier) / "rapport_final.md").write_text("# Sensitive\nPRIVATE_REPORT_CONTENT", encoding="utf-8")
            self.client.get(f"/api/jobs/{identifier}/report/view")
            with self.client.get(f"/api/jobs/{identifier}/report") as response:
                self.assertEqual(response.status_code, 200)
        output = "\n".join(captured.output)
        for expected in ("job_created", "job_cancel_requested", "job_state", "report_download", "report_view", "job_id="):
            self.assertIn(expected, output)
        for private in (secret, "PRIVATE_SERVER_PROMPT", "PRIVATE_REPORT_CONTENT"):
            self.assertNotIn(private, output)

    def test_level_filtering_and_sanitized_traceback(self):
        root_level, app_level = logging.getLogger().level, app.log.level
        self.addCleanup(logging.getLogger().setLevel, root_level)
        self.addCleanup(app.log.setLevel, app_level)
        for value, expected in (("ERROR", logging.ERROR), ("DEBUG", logging.DEBUG), ("invalid", logging.INFO)):
            with patch.dict(os.environ, {"LOG_LEVEL": value}):
                observability.configure_logging()
            self.assertEqual(app.log.level, expected)
        app.log.setLevel(logging.ERROR)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        app.log.addHandler(handler)
        self.addCleanup(app.log.removeHandler, handler)
        observability.event("info_must_be_filtered")
        self.assertEqual(stream.getvalue(), "")
        try:
            raise ValueError("PRIVATE_BACKEND_RESPONSE")
        except ValueError as exc:
            observability.failure("controlled_failure", exc, job_id="a" * 32)
        output = stream.getvalue()
        self.assertIn("Traceback", output)
        self.assertIn("controlled_failure", output)
        # Source lines can also contain secrets; the traceback must omit them.
        self.assertNotIn("PRIVATE_BACKEND_RESPONSE", output)

    def test_cancel_is_idempotent_and_completed_jobs_reject_cancellation(self):
        identifier = self.create("Summarize")
        app.store.update_job(identifier, status="running")
        with self.assertLogs(app.log, "INFO") as captured:
            for _ in range(2):
                response = self.client.post(f"/api/jobs/{identifier}/cancel")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json["status"], "running")
        self.assertEqual(sum("job_cancel_requested" in line for line in captured.output), 1)
        app.store.update_job(identifier, status="cancelled")
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/cancel").json["status"], "cancelled")
        with app.store.connect() as con:
            con.execute("UPDATE jobs SET cancel_requested=0 WHERE id=?", (identifier,))
        app.store.update_job(identifier, status="done")
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/cancel").status_code, 409)


if __name__ == "__main__":
    unittest.main()
