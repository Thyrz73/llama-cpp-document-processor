"""Exercise shared backend observations with a fake clock and no network."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pdf-capability-test-"))
os.environ.setdefault(
    "SYSTEM_PROMPT_FILE",
    str(Path(__file__).resolve().parents[1] / "config/system_prompt.example.txt"),
)

import app
from document_processor.config import ConfigurationError
from document_processor.errors import Cancelled, ModelError


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.models = {"data": [{"id": "private/model-one"}]}
        self.props = {
            "default_generation_settings": {"n_ctx": 16384},
            "chat_template": "template-one",
        }
        self.calls = []
        self.unavailable = False
        for target, name, value in (
            (app, "_capability_cache", {"value": None, "expires": 0}),
            (app, "_capability_lock", threading.Lock()),
            (app, "read_system_prompt", lambda: "PRIVATE_PROMPT"),
            (app.time, "time", lambda: self.now + 1000),
            (app.time, "monotonic", lambda: self.now),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("requests.Session.request", side_effect=self.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = app.app.test_client()

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append(path)
        if self.unavailable:
            raise requests.ConnectionError("PRIVATE_BACKEND_ERROR")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            self.models if path == "/v1/models" else self.props
        ).encode()
        response._content_consumed = True
        return response

    def client(self):
        client = app.ModelClient()
        self.addCleanup(client.session.close)
        return client

    def diagnostics(self):
        response = self.http.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PRIVATE", response.get_data(as_text=True))
        self.assertNotIn("private/", response.get_data(as_text=True))
        return response.get_json()["backend"]

    def test_jobs_and_diagnostics_share_properties_without_extending_the_ttl(self):
        first = self.client().inspect()
        self.assertEqual(self.calls, ["/v1/models", "/props"])
        self.now += 29
        self.assertEqual(self.diagnostics(), first)
        next_client = self.client()
        self.assertEqual(next_client.inspect(), first)
        self.assertEqual(next_client.model(), "private/model-one")
        self.assertEqual(self.calls, ["/v1/models", "/props", "/v1/models"])
        self.now += 1
        refreshed = self.diagnostics()
        self.assertNotEqual(first["observed_at"], refreshed["observed_at"])
        self.assertEqual(
            self.calls, ["/v1/models", "/props", "/v1/models", "/v1/models", "/props"]
        )

    def test_diagnostic_inspection_also_supplies_properties_to_the_next_job(self):
        observed = self.diagnostics()
        self.assertEqual(self.client().inspect(), observed)
        self.assertEqual(self.calls, ["/v1/models", "/props", "/v1/models"])

    def test_changed_model_id_refreshes_properties_before_ttl_expiration(self):
        previous = self.client().inspect()
        # Identical public basenames still represent different full engine IDs.
        self.models = {"data": [{"id": "other/model-one"}]}
        self.props = {
            "default_generation_settings": {"n_ctx": 8192},
            "chat_template": "template-two",
        }
        current = self.client().inspect()
        self.assertEqual(current["model"], previous["model"])
        self.assertEqual(current["context_tokens"], 8192)
        self.assertNotEqual(current["template_hash"], previous["template_hash"])
        self.assertEqual(self.calls, ["/v1/models", "/props"] * 2)

    def test_same_model_properties_are_refreshed_at_the_success_ttl(self):
        client = self.client()
        client.inspect()
        self.props = {"default_generation_settings": {"n_ctx": 4096}}
        self.now += app.CAPABILITY_CACHE_SECONDS
        self.assertEqual(client.inspect()["context_tokens"], 4096)
        self.assertEqual(self.calls, ["/v1/models", "/props"] * 2)

    def test_backend_url_or_credentials_cannot_reuse_another_backend_observation(self):
        self.diagnostics()
        for name, value in (
            ("LLAMA_BASE", "http://other.invalid"),
            ("KEY", "PRIVATE_NEW_KEY"),
        ):
            with self.subTest(name=name), patch.object(app, name, value):
                self.assertIsNone(app.cached_capabilities())
                before = len(self.calls)
                self.diagnostics()
                self.assertEqual(len(self.calls), before + 2)

    def test_failures_have_a_short_ttl_and_recover_without_guessing_context(self):
        self.client().inspect()
        self.unavailable = True
        failed = self.client().inspect()
        self.assertEqual(
            (failed["status"], failed["context_tokens"]), ("unavailable", None)
        )
        self.unavailable = False
        self.now += app.CAPABILITY_FAILURE_CACHE_SECONDS - 1
        self.assertEqual(self.diagnostics(), failed)
        self.assertEqual(self.client().inspect(), failed)
        self.assertEqual(self.calls, ["/v1/models", "/props", "/v1/models"])
        self.now += 1
        self.assertEqual(self.diagnostics()["context_tokens"], 16384)
        self.assertEqual(
            self.calls, ["/v1/models", "/props", "/v1/models", "/v1/models", "/props"]
        )

    def test_invalid_models_are_sanitized_and_do_not_keep_a_known_context(self):
        self.client().inspect()
        for payload in (
            {"data": {"0": {"id": "invalid"}}},
            {"data": [{"id": ""}]},
            {"data": [None]},
        ):
            with self.subTest(payload=payload):
                self.models = payload
                observed = self.client().inspect()
                self.assertEqual(
                    (observed["status"], observed["context_tokens"]),
                    ("unavailable", None),
                )
                self.now += app.CAPABILITY_FAILURE_CACHE_SECONDS
        self.models = {"data": [{"id": "restored"}]}
        self.assertEqual(self.client().inspect()["context_tokens"], 16384)

    def test_invalid_or_missing_properties_use_the_short_ttl_and_unknown_warning(self):
        for props in ([], {"default_generation_settings": {"n_ctx": "16384"}}, {}):
            with self.subTest(props=props):
                self.props = props
                observed = self.client().inspect()
                self.assertIsNone(observed["context_tokens"])
                preview = self.http.post("/api/config/preview", json={}).get_json()
                self.assertIn(
                    "engine_context_unknown", preview["configuration"]["warnings"]
                )
                self.now += app.CAPABILITY_FAILURE_CACHE_SECONDS
        self.props = {"default_generation_settings": {"n_ctx": 16384}}
        self.assertEqual(self.client().inspect()["context_tokens"], 16384)

    def test_request_and_generation_failures_invalidate_but_cancellation_and_validation_do_not(
        self,
    ):
        for exc in (
            ModelError("stream_error", "Safe error."),
            Cancelled(),
            ConfigurationError("context_insufficient", "Safe validation."),
        ):
            with self.subTest(error=type(exc).__name__):
                client = self.client()
                client.inspect()
                with (
                    patch.object(app.BackendClient, "generate", side_effect=exc),
                    self.assertRaises(type(exc)),
                ):
                    client.generate("Analyze", "Source", 10)
                self.assertEqual(
                    app.cached_capabilities() is None, isinstance(exc, ModelError)
                )
        self.unavailable = True
        with self.assertRaises(ModelError):
            client.json_request("GET", "/props")
        self.assertIsNone(app.cached_capabilities())

    def test_cancellation_is_checked_before_using_a_cached_inspection(self):
        self.diagnostics()
        previous = app.cached_capabilities()
        client = self.client()
        client.cancel_check = lambda: True
        with self.assertRaises(Cancelled):
            client.inspect()
        self.assertEqual(app.cached_capabilities(), previous)
        self.assertEqual(self.calls, ["/v1/models", "/props"])

    def test_invalid_generation_and_tokenizer_responses_discard_known_capabilities(
        self,
    ):
        client = self.client()
        client.inspect()
        with (
            patch.object(
                client, "fits", return_value=(True, 10, "estimated_template", 384)
            ),
            patch.object(
                client, "stream_lines", return_value=iter([b"data: {invalid"])
            ),
            self.assertRaises(ModelError) as raised,
        ):
            client.generate("Analyze", "Source", 10)
        self.assertEqual(raised.exception.code, "invalid_stream")
        self.assertIsNone(app.cached_capabilities())
        client.inspect()
        self.props = {"tokens": None}
        with self.assertRaises(ModelError) as raised:
            client.tokenize("Source")
        self.assertEqual(raised.exception.code, "invalid_tokenizer")
        self.assertIsNone(app.cached_capabilities())

    def test_cancellation_while_waiting_for_the_inspection_lock_keeps_its_owner(self):
        class BusyLock:
            def acquire(lock, **kwargs):
                client.cancel_check = lambda: True
                return False

            def release(lock):
                self.fail("Cancellation must not release another inspection's lock")

        client = self.client()
        with (
            patch.object(app, "_capability_lock", BusyLock()),
            self.assertRaises(Cancelled),
        ):
            client.inspect()
        self.assertEqual(self.calls, [])

    def test_cached_values_are_copied_and_routes_share_the_same_context_observation(
        self,
    ):
        self.diagnostics()
        value = app.cached_capabilities()
        value["context_tokens"] = 1
        self.assertEqual(app.cached_capabilities()["context_tokens"], 16384)
        client = self.client()
        client.inspect()["sampling"]["temperature"] = 1
        self.assertEqual(app.cached_capabilities()["sampling"], {})
        before = len(self.calls)
        profiles = self.http.get("/api/profiles").get_json()
        preview = self.http.post("/api/config/preview", json={}).get_json()
        for config in (profiles["defaults"], preview["configuration"]):
            self.assertNotIn("engine_context_unknown", config["warnings"])
        self.assertEqual(len(self.calls), before)
        self.now += app.CAPABILITY_CACHE_SECONDS
        profiles = self.http.get("/api/profiles").get_json()
        self.assertIsNone(profiles["capabilities"])
        self.assertIn("engine_context_unknown", profiles["defaults"]["warnings"])
        self.assertEqual(len(self.calls), before)

    def test_profiles_remain_available_when_known_context_requires_smaller_budgets(
        self,
    ):
        self.props = {"default_generation_settings": {"n_ctx": 1024}}
        self.diagnostics()
        profiles = self.http.get("/api/profiles")
        self.assertEqual(profiles.status_code, 200)
        self.assertNotIn(
            "engine_context_unknown", profiles.get_json()["defaults"]["warnings"]
        )
        preview = self.http.post("/api/config/preview", json={})
        self.assertEqual(preview.status_code, 400)
        self.assertEqual(preview.get_json()["error_code"], "context_insufficient")


if __name__ == "__main__":
    unittest.main()
