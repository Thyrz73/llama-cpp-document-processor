"""Verify attempt identities through simulated HTTP, real persistence and retries."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

# This module also prepares the isolated app import environment.
import test_encoding as encoding

import app
from document_processor import extraction
from document_processor.artifacts import Artifacts
from document_processor.errors import ModelError, ProcessingError
from document_processor.jobs import JobStore
from document_processor.worker import Worker


class IdentityAdapter(encoding.ModelAdapter):
    def __init__(self):
        super().__init__("Measured value: 25 degrees. [Page 1]")
        self.model_id = "private/model-one"
        self.props = {
            "default_generation_settings": {
                "n_ctx": 16384,
                "params": {
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "top_k": 40,
                    "min_p": 0.05,
                    "repeat_penalty": 1.1,
                },
            },
            "chat_template": "original-template",
            "version": "test-1",
        }
        self.requests = []
        self.unavailable = None
        self.on_models = lambda: None

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        self.requests.append(path)
        if path == self.unavailable:
            raise requests.ConnectionError("PRIVATE_BACKEND_ERROR")
        if path == "/v1/models":
            self.on_models()
            value = {"data": [{"id": self.model_id}]}
        elif path == "/props":
            value = self.props
        else:
            return super().send(request, **kwargs)
        return encoding.response_for(json.dumps(value).encode(), "application/json")


class BackendIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="backend-identity-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = IdentityAdapter()
        real_session = requests.Session

        def session():
            result = real_session()
            result.mount("http://", self.adapter)
            result.mount("https://", self.adapter)
            return result

        for target, name, value in (
            (app, "store", JobStore(self.root)),
            (app, "_capability_cache", {"value": None, "expires": 0}),
            (app, "read_system_prompt", lambda: "PRIVATE_SYSTEM_PROMPT"),
            (app.time, "monotonic", lambda: 100.0),
            (requests, "Session", session),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "requests.adapters.HTTPAdapter.send",
            side_effect=AssertionError("Real network forbidden"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        app.store.initialize()
        self.addCleanup(app.store.close)
        self.http = app.app.test_client()

    def create(self):
        response = self.http.post(
            "/api/jobs",
            data={
                "file": (io.BytesIO(b"%PDF-test"), "study.pdf"),
                "question": "Summarize the measurements.",
                "options": json.dumps({"overrides": {"chunk_tokens": 128}}),
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def run_attempt(self, identifier, run=None):
        app.store.update_job(identifier, status="running")
        pages = [
            {"page": i + 1, "text": self.adapter.text + "\n\n" + "padding " * 70}
            for i in range(5)
        ]
        with patch.object(extraction, "extract_pdf", return_value=(pages, 5, [], [])):
            (run or app.analyze_job)(identifier)

    def seed_retry(self):
        identifier = self.create()
        self.run_attempt(identifier)
        app.store.update_job(identifier, status="failed")
        response = self.http.post(f"/api/jobs/{identifier}/retry")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(app._capability_cache["expires"], app.time.monotonic())
        return identifier

    def artifacts(self, identifier):
        directory = app.store.job_dir(identifier) / "artifacts"
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def assert_refused(
        self, identifier, code="identity_changed", exception=ProcessingError
    ):
        before = self.artifacts(identifier)
        snapshot = app.store.load_config(identifier)
        generations = len(self.adapter.completions)
        with (
            patch.object(
                Artifacts,
                "read",
                side_effect=AssertionError("No reuse before identity validation"),
            ) as read,
            self.assertRaises(exception) as raised,
        ):
            self.run_attempt(identifier)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("PRIVATE", str(raised.exception))
        read.assert_not_called()
        self.assertEqual(self.artifacts(identifier), before)
        self.assertEqual(app.store.load_config(identifier), snapshot)
        self.assertEqual(len(self.adapter.completions), generations)
        metrics = app.store.metrics(identifier)
        self.assertEqual(metrics["runs"][0]["status"], "failed")
        self.assertNotEqual(app.store.get_job(identifier)["status"], "done")

    def test_same_id_and_unchanged_properties_reuse_nine_completed_artifacts(self):
        identifier = self.seed_retry()
        original = self.artifacts(identifier)
        snapshot = app.store.load_config(identifier)
        generations = len(self.adapter.completions)
        requests_before = len(self.adapter.requests)
        self.run_attempt(identifier)
        self.assertEqual(app.store.get_job(identifier)["status"], "done")
        self.assertEqual(app.store.metrics(identifier)["summary"]["reused_steps"], 9)
        self.assertEqual(len(self.adapter.completions), generations)
        inspections = [
            path
            for path in self.adapter.requests[requests_before:]
            if path in ("/v1/models", "/props")
        ]
        self.assertEqual(inspections, ["/v1/models", "/props"])
        self.assertEqual(self.artifacts(identifier), original)
        self.assertEqual(app.store.load_config(identifier), snapshot)

    def test_same_id_and_changed_template_before_ttl_refuse_reuse(self):
        identifier = self.seed_retry()
        self.adapter.props["chat_template"] = "changed-template"
        self.assert_refused(identifier)

    def test_changed_sampling_parameters_before_ttl_refuse_reuse(self):
        original = copy.deepcopy(self.adapter.props)
        for name, value in (
            ("temperature", 0.4),
            ("top_p", 0.8),
            ("top_k", 30),
            ("min_p", 0.1),
            ("repeat_penalty", 1.2),
        ):
            with self.subTest(parameter=name):
                self.adapter.props = copy.deepcopy(original)
                identifier = self.seed_retry()
                self.adapter.props["default_generation_settings"]["params"][name] = (
                    value
                )
                self.assert_refused(identifier)

    def test_changed_full_model_id_refuses_reuse_even_with_the_same_public_name(self):
        identifier = self.seed_retry()
        self.adapter.model_id = "other/model-one"
        self.assert_refused(identifier)

    def test_unavailable_model_or_properties_cannot_validate_old_artifacts(self):
        for path in ("/v1/models", "/props"):
            with self.subTest(path=path):
                self.adapter.unavailable = None
                identifier = self.seed_retry()
                self.adapter.unavailable = path
                before = len(self.adapter.requests)
                self.assert_refused(
                    identifier,
                    "backend_unavailable"
                    if path == "/v1/models"
                    else "identity_changed",
                )
                self.assertEqual(
                    self.adapter.requests[before:],
                    ["/v1/models"]
                    if path == "/v1/models"
                    else ["/v1/models", "/props"],
                )

    def test_missing_template_or_sampling_refuses_reuse_without_rewriting_manifest(
        self,
    ):
        original = copy.deepcopy(self.adapter.props)
        for field in ("chat_template", "params"):
            with self.subTest(field=field):
                self.adapter.props = copy.deepcopy(original)
                identifier = self.seed_retry()
                if field == "chat_template":
                    del self.adapter.props[field]
                else:
                    del self.adapter.props["default_generation_settings"][field]
                self.assert_refused(identifier)

    def test_legacy_manifest_with_unknown_identity_is_not_upgraded_to_allow_reuse(self):
        identifier = self.seed_retry()
        path = next(app.store.job_dir(identifier).glob("artifacts/*/manifest.json"))
        manifest = json.loads(path.read_text())
        del manifest["identity"]["engine_sampling_hash"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_refused(identifier)

    def test_invalid_sampling_value_cannot_be_discarded_to_match_an_old_identity(self):
        self.adapter.props["default_generation_settings"]["params"] = {
            "temperature": 0.2
        }
        identifier = self.seed_retry()
        self.adapter.props["default_generation_settings"]["params"]["top_p"] = (
            "PRIVATE_INVALID_VALUE"
        )
        self.assert_refused(identifier)

    def test_unknown_properties_on_both_attempts_do_not_prove_identity_compatibility(
        self,
    ):
        del self.adapter.props["default_generation_settings"]["params"]
        identifier = self.seed_retry()
        self.assert_refused(identifier)

    def test_fresh_attempt_bypasses_a_cached_ui_failure_after_backend_recovers(self):
        identifier = self.seed_retry()
        self.adapter.unavailable = "/v1/models"
        client = app.ModelClient()
        self.addCleanup(client.session.close)
        app.invalidate_capabilities(client)
        failed = self.http.get("/api/diagnostics").get_json()["backend"]
        self.assertEqual(failed["status"], "unavailable")
        self.adapter.unavailable = None
        self.run_attempt(identifier)
        self.assertEqual(app.store.get_job(identifier)["status"], "done")
        self.assertEqual(app.store.metrics(identifier)["summary"]["reused_steps"], 9)

    def test_attempt_checks_properties_once_and_supplies_the_ui_cache(self):
        identifier = self.create()
        self.run_attempt(identifier)
        self.assertEqual(len(self.adapter.completions), 7)
        self.assertEqual(self.adapter.requests.count("/v1/models"), 1)
        self.assertEqual(self.adapter.requests.count("/props"), 1)
        before = len(self.adapter.requests)
        for _ in range(3):
            self.assertEqual(self.http.get("/api/diagnostics").status_code, 200)
        self.assertEqual(len(self.adapter.requests), before)

    def test_cancelled_identity_check_does_not_reuse_artifacts_or_corrupt_ui_cache(
        self,
    ):
        identifier = self.seed_retry()
        original = self.artifacts(identifier)
        cached = app.cached_capabilities()
        runner = Worker(app.store, app.analyze_job)

        def cancel():
            app.store.update_job(identifier, cancel_requested=1)
            runner.notify_cancelled(identifier)

        self.adapter.on_models = cancel
        with patch.object(
            Artifacts, "read", side_effect=AssertionError("No reuse on cancellation")
        ):
            self.run_attempt(identifier, runner.run_claimed)
        self.assertEqual(app.store.get_job(identifier)["status"], "cancelled")
        self.assertEqual(
            app.store.metrics(identifier)["runs"][0]["status"], "cancelled"
        )
        self.assertEqual(self.artifacts(identifier), original)
        self.assertEqual(app.cached_capabilities(), cached)
        self.assertFalse(app._capability_lock.locked())

    def test_unavailable_first_attempt_makes_no_duplicate_model_request(self):
        identifier = self.create()
        self.adapter.unavailable = "/v1/models"
        with self.assertRaises(ModelError) as raised:
            self.run_attempt(identifier)
        self.assertEqual(raised.exception.code, "backend_unavailable")
        self.assertEqual(self.adapter.requests, ["/v1/models"])
        self.assertEqual(app.cached_capabilities()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
