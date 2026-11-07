"""Real injected clients, bounded concurrency and shared diagnostic/identity state."""

import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from document_processor import backend
from document_processor.application import create_app
from document_processor.artifacts import Artifacts
from document_processor.backend import BackendClient, DeploymentModelClient
from document_processor.errors import ModelError, ProcessingError
from document_processor.pipeline import JobRun
from document_processor.settings import Settings


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="diagnostic-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        with patch.dict(os.environ, {}, clear=True):
            self.settings = replace(
                Settings.from_env(),
                data_dir=self.root / "data",
                llama_base_url="http://backend.invalid",
                api_key="PRIVATE_API_KEY",
                system_prompt_file=self.root / "PRIVATE_PROMPT_DO_NOT_READ.txt",
            )
        self.now = 100.0
        self.props = {
            "default_generation_settings": {
                "n_ctx": 16384,
                "params": {"temperature": 0.2},
            },
            "chat_template": "PRIVATE_TEMPLATE",
        }
        self.unavailable = False
        self.calls = []
        self.clients = []
        self.threads = []
        self.cancelled = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.records_lock = threading.Lock()
        self.inflight = self.max_inflight = 0
        for target, name, value in (
            (backend.time, "monotonic", lambda: self.now),
            (backend.time, "time", lambda: 1000 + self.now),
            (requests.Session, "request", Mock(side_effect=self.request)),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.application = self.make_app()
        self.addCleanup(self.finish_threads)

    def make_app(self, *, cached=True, injected=True):
        def factory(config):
            kwargs = {"capabilities": self.services.capabilities} if cached else {}
            cls = DeploymentModelClient if cached else BackendClient
            client = cls(
                self.settings.llama_base_url,
                self.settings.api_key,
                config,
                config["system_prompt"],
                **kwargs,
            )
            client.cancel_check = self.cancelled.is_set
            client.session.close = Mock(wraps=client.session.close)
            with self.records_lock:
                self.clients.append(client)
            return client

        application = create_app(
            self.settings, client_factory=factory if injected else None
        )
        self.services = application.extensions["document_processor"]
        self.addCleanup(self.services.store.close)
        return application

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.assertEqual(method, "GET")
        self.assertIn(path, ("/v1/models", "/props"))
        with self.records_lock:
            self.calls.append(path)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if path == "/v1/models":
                self.entered.set()
                if not self.release.wait(2):
                    raise AssertionError("Test backend was not released")
            if self.unavailable:
                raise requests.ConnectionError("PRIVATE_BACKEND_ERROR")
            value = (
                {"data": [{"id": "private/model-one"}]}
                if path == "/v1/models"
                else self.props
            )
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(value).encode()
            response._content_consumed = True
            return response
        finally:
            with self.records_lock:
                self.inflight -= 1

    def start_task(self, callback):
        done, results = threading.Event(), []

        def run():
            try:
                results.append(callback())
            except BaseException as exc:
                results.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return done, results

    def result(self, task):
        done, results = task
        self.assertTrue(done.wait(2), "Inspection deadlocked")
        if isinstance(results[0], BaseException):
            raise results[0]
        return results[0]

    def finish_threads(self):
        # Cancellation releases the original regression's client lock wait too.
        self.cancelled.set()
        self.release.set()
        for thread in self.threads:
            thread.join(2)
        self.assertFalse(any(thread.is_alive() for thread in self.threads))

    def diagnostic_request(self):
        with self.application.test_client() as http:
            response = http.get("/api/diagnostics")
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            for secret in (
                "PRIVATE",
                "private/model-one",
                str(self.settings.system_prompt_file),
            ):
                self.assertNotIn(secret, text)
            payload = response.get_json()
            self.assertEqual(payload["engine_settings"], "read_only")
            return payload["backend"]

    def diagnostics(self):
        return self.result(self.start_task(self.diagnostic_request))

    def analysis_client(self):
        client = self.services.client_factory(
            {**self.settings.job_defaults(), "system_prompt": "PRIVATE_ANALYSIS_PROMPT"}
        )
        client.cancel_check = self.cancelled.is_set
        self.addCleanup(client.session.close)
        return client

    def test_real_injected_client_fills_cache_without_prompt_reads_and_reuses_it(self):
        first = self.diagnostics()
        self.assertEqual(first["context_tokens"], 16384)
        self.assertIsInstance(self.clients[0], DeploymentModelClient)
        self.assertEqual(self.clients[0].prompt, "")
        self.assertEqual(self.calls, ["/v1/models", "/props"])
        self.assertEqual(self.diagnostics(), first)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.calls, ["/v1/models", "/props"])
        self.clients[0].session.close.assert_called_once()
        self.assertFalse(self.services.capabilities.lock.locked())

    def test_success_ttl_is_not_extended_by_reads_and_refreshes_at_expiration(self):
        first = self.diagnostics()
        self.now = 129
        self.assertEqual(self.diagnostics(), first)
        self.assertEqual(self.services.capabilities.entry["expires"], 130)
        self.props["default_generation_settings"]["n_ctx"] = 8192
        self.now = 130
        refreshed = self.diagnostics()
        self.assertEqual(refreshed["context_tokens"], 8192)
        self.assertNotEqual(refreshed["observed_at"], first["observed_at"])
        self.assertEqual(self.calls, ["/v1/models", "/props"] * 2)
        self.assertEqual(self.services.capabilities.entry["expires"], 160)

    def test_backend_failure_has_five_second_ttl_and_recovers(self):
        self.diagnostics()
        self.now = 130
        self.unavailable = True
        failed = self.diagnostics()
        self.assertEqual(
            (failed["status"], failed["context_tokens"]), ("unavailable", None)
        )
        self.assertEqual(self.services.capabilities.entry["expires"], 135)
        self.unavailable = False
        self.now = 134
        self.assertEqual(self.diagnostics(), failed)
        self.assertEqual(self.calls, ["/v1/models", "/props", "/v1/models"])
        self.now = 135
        self.assertEqual(self.diagnostics()["context_tokens"], 16384)
        self.assertEqual(
            self.calls, ["/v1/models", "/props", "/v1/models", "/v1/models", "/props"]
        )

    def test_unexpected_backend_error_keeps_http_errors_private_and_releases_resources(
        self,
    ):
        def request():
            with self.application.test_client() as http:
                response = http.get("/api/diagnostics")
                return response.status_code, response.get_json()

        with patch.object(
            requests.Session,
            "request",
            side_effect=RuntimeError("PRIVATE_INTERNAL_ERROR"),
        ):
            status, body = self.result(self.start_task(request))
        self.assertEqual(status, 500)
        self.assertEqual(
            body, {"error": "An internal error occurred.", "error_code": "http_500"}
        )
        self.clients[0].session.close.assert_called_once()
        self.assertFalse(self.services.capabilities.lock.locked())
        self.assertIsNone(self.services.capabilities.get())
        self.assertEqual(self.diagnostics()["context_tokens"], 16384)

    def test_unknown_context_also_refreshes_after_five_seconds(self):
        self.props = {}
        self.assertIsNone(self.diagnostics()["context_tokens"])
        self.assertEqual(self.services.capabilities.entry["expires"], 105)
        self.props = {"default_generation_settings": {"n_ctx": 8192}}
        self.now = 105
        self.assertEqual(self.diagnostics()["context_tokens"], 8192)
        self.assertEqual(self.calls, ["/v1/models", "/props"] * 2)

    def concurrent_diagnostics(self):
        self.release.clear()
        tasks = [self.start_task(self.diagnostic_request) for _ in range(5)]
        try:
            self.assertTrue(self.entered.wait(1))
        finally:
            self.release.set()
        values = [self.result(task) for task in tasks]
        self.assertTrue(all(value == values[0] for value in values))
        self.assertEqual(self.calls, ["/v1/models", "/props"])
        self.assertEqual(self.max_inflight, 1)
        for client in self.clients:
            client.session.close.assert_called_once()
        self.assertFalse(self.services.capabilities.lock.locked())

    def test_simultaneous_real_cached_clients_coalesce_one_inspection(self):
        self.concurrent_diagnostics()

    def test_simultaneous_raw_injected_clients_also_coalesce_one_inspection(self):
        self.application = self.make_app(cached=False)
        self.concurrent_diagnostics()

    def test_standard_diagnostics_share_properties_with_the_next_analysis(self):
        self.application = self.make_app(injected=False)
        observed = self.diagnostics()
        client = self.analysis_client()
        self.assertEqual(client.inspect(), observed)
        self.assertEqual(self.calls, ["/v1/models", "/props", "/v1/models"])
        self.assertEqual(self.diagnostics(), observed)
        self.assertEqual(len(self.calls), 3)

    def test_diagnostic_and_fresh_identity_inspections_share_the_lock(self):
        self.release.clear()
        diagnostic = self.start_task(self.diagnostic_request)
        try:
            self.assertTrue(self.entered.wait(1))
            client = self.analysis_client()
            identity = self.start_task(client.inspect_identity)
        finally:
            self.release.set()
        self.assertEqual(self.result(diagnostic)["status"], "available")
        self.assertEqual(self.result(identity)["status"], "available")
        self.assertEqual(self.calls, ["/v1/models", "/props"] * 2)
        self.assertEqual(self.max_inflight, 1)

    def test_fresh_identity_rejects_changed_artifacts_despite_warm_diagnostic_cache(
        self,
    ):
        self.diagnostics()
        run = JobRun.__new__(JobRun)
        run.job_id = "a" * 32
        run.model = self.analysis_client()
        run.recorder = Mock()
        run._phase = Mock()
        run.artifacts = Artifacts(self.root / "job", {"pipeline_version": 2})
        run._prepare()
        run.artifacts.write("cached.md", "Previously generated text", {})
        manifest = run.artifacts.path.read_bytes()
        self.props["chat_template"] = "PRIVATE_CHANGED_TEMPLATE"
        with (
            patch.object(
                run.artifacts, "read", side_effect=AssertionError("Unsafe reuse")
            ) as read,
            self.assertRaises(ProcessingError) as caught,
        ):
            run._prepare()
            run.artifacts.read("cached.md", {})
        self.assertEqual(caught.exception.code, "identity_changed")
        read.assert_not_called()
        self.assertEqual(run.artifacts.path.read_bytes(), manifest)
        self.assertEqual(self.calls, ["/v1/models", "/props"] * 3)

    def test_fresh_identity_bypasses_cached_failure_and_still_propagates_new_failure(
        self,
    ):
        self.unavailable = True
        self.assertEqual(self.diagnostics()["status"], "unavailable")
        self.unavailable = False
        client = self.analysis_client()
        self.assertEqual(client.inspect_identity()["context_tokens"], 16384)
        self.assertEqual(self.calls, ["/v1/models", "/v1/models", "/props"])
        self.unavailable = True
        with self.assertRaises(ModelError) as caught:
            client.inspect_identity()
        self.assertEqual(caught.exception.code, "backend_unavailable")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.services.capabilities.get()["status"], "unavailable")
        self.assertFalse(self.services.capabilities.lock.locked())


if __name__ == "__main__":
    unittest.main()
