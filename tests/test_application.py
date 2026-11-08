"""Factory isolation and HTTP/lifecycle contracts, using injected collaborators."""

import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from flask import url_for

from document_processor import __version__, pipeline
from document_processor.application import create_app
from document_processor.backend import BackendClient, DeploymentModelClient
from document_processor.database import SchemaVersionError
from document_processor.jobs import SOURCE_FILE, JobStore
from document_processor.lifecycle import run_server
from document_processor.observability import log
from document_processor.settings import Settings
from document_processor.worker import Worker

ROOT = Path(__file__).resolve().parents[1]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="factory-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("PRIVATE_SYSTEM_PROMPT", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = replace(
                Settings.from_env(),
                data_dir=self.root / "data",
                system_prompt_file=self.prompt,
            )

    def application(self, *, settings=None, client_factory=None, worker=None):
        settings = self.settings if settings is None else settings
        store = JobStore(settings.data_dir)
        self.addCleanup(store.close)
        application = create_app(settings, store, client_factory, worker=worker)
        return application, application.extensions["document_processor"]

    def ready(self, **kwargs):
        application, services = self.application(**kwargs)
        services.store.initialize()
        return application.test_client(), services

    def upload(self, client, **data):
        return client.post(
            "/api/jobs",
            data={"file": (io.BytesIO(b"%PDF-test"), "example.pdf"), **data},
        )

    def test_construction_and_health_do_not_initialize_storage_or_start_work(self):
        factory = Mock(side_effect=AssertionError("Client constructed during factory"))
        with (
            patch.object(
                JobStore,
                "initialize",
                side_effect=AssertionError("Database initialized"),
            ),
            patch.object(Worker, "start", side_effect=AssertionError("Worker started")),
            patch(
                "requests.Session", side_effect=AssertionError("HTTP session opened")
            ),
        ):
            application, services = self.application(client_factory=factory)
            self.assertFalse(self.settings.data_dir.exists())
            self.assertIsNone(services.worker._thread)
            response = application.test_client().get("/api/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {"status": "ok", "version": __version__})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertFalse(self.settings.data_dir.exists())
            factory.assert_not_called()
        self.assertEqual(set(application.blueprints), {"jobs", "reports", "system"})
        self.assertEqual(
            application.config["MAX_CONTENT_LENGTH"], 20 * 1024 * 1024 + 65536
        )

    def test_all_routes_and_http_methods_are_preserved(self):
        application, _ = self.application()
        expected = {
            ("/", "GET"),
            ("/api/health", "GET"),
            ("/api/profiles", "GET"),
            ("/api/config/preview", "POST"),
            ("/api/diagnostics", "GET"),
            ("/api/jobs", "GET"),
            ("/api/jobs/<job_id>", "GET"),
            ("/api/jobs/<job_id>/delete", "POST"),
            ("/api/jobs/delete", "POST"),
            ("/api/jobs", "POST"),
            ("/api/jobs/<job_id>/live", "GET"),
            ("/api/jobs/<job_id>/metrics", "GET"),
            ("/api/jobs/<job_id>/retry", "POST"),
            ("/api/jobs/<job_id>/resume", "POST"),
            ("/api/jobs/<job_id>/cancel", "POST"),
            ("/api/jobs/<job_id>/report", "GET"),
            ("/api/jobs/<job_id>/report/view", "GET"),
            ("/static/<path:filename>", "GET"),
        }
        actual = {
            (rule.rule, method)
            for rule in application.url_map.iter_rules()
            for method in rule.methods - {"HEAD", "OPTIONS"}
        }
        self.assertEqual(actual, expected)
        with application.test_request_context():
            self.assertEqual(url_for("system.index"), "/")
            self.assertEqual(
                url_for("reports.report", job_id="a" * 32),
                "/api/jobs/" + "a" * 32 + "/report",
            )
        for rule in application.url_map.iter_rules():
            if "GET" in rule.methods:
                self.assertIn("HEAD", rule.methods)
            self.assertIn("OPTIONS", rule.methods)
        self.assertEqual(application.test_client().put("/api/health").status_code, 405)

    def test_applications_isolate_stores_workers_settings_and_capabilities(self):
        first_factory, second_factory = Mock(), Mock()
        first, first_services = self.application(client_factory=first_factory)
        second_settings = replace(
            self.settings,
            data_dir=self.root / "second",
            max_upload_mb=3,
            live_output_enabled=False,
        )
        second, second_services = self.application(
            settings=second_settings, client_factory=second_factory
        )
        self.assertIsNot(first_services.store, second_services.store)
        self.assertIsNot(first_services.worker, second_services.worker)
        self.assertIsNot(
            first_services.capabilities.lock, second_services.capabilities.lock
        )
        first_services.capabilities.remember(
            {"status": "available", "context_tokens": 8192}
        )
        self.assertIsNone(second_services.capabilities.get())
        with first.test_request_context():
            self.assertEqual(
                first.config["MAX_CONTENT_LENGTH"], 20 * 1024 * 1024 + 65536
            )
        self.assertEqual(second.config["MAX_CONTENT_LENGTH"], 3 * 1024 * 1024 + 65536)
        for services, factory in (
            (first_services, first_factory),
            (second_services, second_factory),
        ):
            check = Mock(return_value=False)
            with patch.object(pipeline, "run") as run:
                services.analyze_job("a" * 32, check)
            runtime = run.call_args.args[1]
            self.assertIs(runtime.store, services.store)
            self.assertIs(runtime.model_client, factory)
            self.assertIs(runtime.cancellation_requested, check)
            self.assertEqual(runtime.live_output, services.settings.live_output_enabled)
            self.assertEqual(
                runtime.new_snapshot({})["system_prompt"], "PRIVATE_SYSTEM_PROMPT"
            )
        first_factory.assert_not_called()
        second_factory.assert_not_called()

    def test_injected_apps_do_not_observe_legacy_global_patches(self):
        import app

        application, services = self.application()
        with (
            patch.object(
                app, "store", Mock(side_effect=AssertionError("Legacy store"))
            ),
            patch.object(app, "MAX_UPLOAD_MB", 1),
            patch.object(app, "read_system_prompt", return_value="LEGACY_PROMPT"),
        ):
            self.assertIs(services.settings, self.settings)
            self.assertEqual(
                services.new_snapshot({})["system_prompt"], "PRIVATE_SYSTEM_PROMPT"
            )
            self.assertEqual(
                application.config["MAX_CONTENT_LENGTH"],
                self.settings.max_content_length,
            )

    def test_system_json_security_headers_and_errors_are_stable(self):
        client, services = self.ready()
        profiles = client.get("/api/profiles")
        self.assertEqual(
            set(profiles.json),
            {"profiles", "defaults", "capabilities", "thinking_supported"},
        )
        self.assertEqual(profiles.json["defaults"]["profile"], "legacy")
        self.assertIsNone(profiles.json["capabilities"])
        preview = client.post("/api/config/preview", json={"profile": "low"})
        self.assertEqual(
            set(preview.json), {"configuration", "capabilities", "validation"}
        )
        self.assertEqual(preview.json["validation"], "preliminary")
        self.assertEqual(preview.json["configuration"]["profile"], "low")
        invalid = client.post("/api/config/preview", json=[])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            invalid.json,
            {
                "error_code": "invalid_options",
                "error": "Options must be a JSON object.",
            },
        )
        for response in (profiles, preview, invalid, client.get("/api/unknown")):
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertNotIn("Set-Cookie", response.headers)
            self.assertNotIn("PRIVATE_", response.get_data(as_text=True))
        self.assertIsNone(services.worker._thread)

    def test_request_failures_and_origin_checks_keep_private_details_out(self):
        client, services = self.ready()
        for origin in ("https://foreign.invalid", "ftp://localhost"):
            response = client.post(
                "/api/jobs/delete", json={"ids": []}, headers={"Origin": origin}
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                response.json,
                {
                    "error_code": "origin_rejected",
                    "error": "Cross-origin writes are not allowed.",
                },
            )
        response = client.post(
            "/api/jobs/delete", json={"ids": []}, headers={"Origin": "http://localhost"}
        )
        self.assertEqual(response.status_code, 400)
        with (
            patch.object(
                services.store, "list_jobs", side_effect=OSError("PRIVATE_FILE_PATH")
            ),
            self.assertLogs(log, "ERROR") as captured,
        ):
            response = client.get("/api/jobs")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json,
            {"error": "An internal error occurred.", "error_code": "http_500"},
        )
        self.assertNotIn("PRIVATE_FILE_PATH", "\n".join(captured.output))
        self.assertIn('endpoint="list_jobs"', "\n".join(captured.output))

    def test_injected_worker_handles_upload_cancel_retry_and_deletion(self):
        worker = Mock(spec=Worker)
        client, services = self.ready(worker=worker)
        response = self.upload(client)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json), {"id", "status", "configuration"})
        identifier = response.json["id"]
        worker.wake.assert_called_once_with()
        snapshot = services.store.load_config(identifier)
        detail = client.get(f"/api/jobs/{identifier}")
        self.assertEqual(detail.json["question_custom"], 0)
        self.assertNotIn("PRIVATE_", detail.get_data(as_text=True))
        metrics = client.get(f"/api/jobs/{identifier}/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertEqual(client.post(f"/api/jobs/{identifier}/retry").status_code, 409)
        cancelled = client.post(f"/api/jobs/{identifier}/cancel")
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(
            cancelled.json, {"status": "cancelled", "cancel_requested": True}
        )
        worker.notify_cancelled.assert_called_once_with(identifier)
        self.assertEqual(client.post(f"/api/jobs/{identifier}/cancel").status_code, 200)
        retried = client.post(f"/api/jobs/{identifier}/retry")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json, {"status": "queued", "warnings": []})
        self.assertEqual(services.store.load_config(identifier), snapshot)
        immutable = client.post(
            f"/api/jobs/{identifier}/retry", json={"profile": "low"}
        )
        self.assertEqual(
            (immutable.status_code, immutable.json["error_code"]),
            (400, "immutable_config"),
        )
        self.assertEqual(client.post(f"/api/jobs/{identifier}/delete").status_code, 409)
        client.post(f"/api/jobs/{identifier}/cancel")
        removed = client.post(f"/api/jobs/{identifier}/delete")
        self.assertEqual(
            removed.json, {"id": identifier, "deleted": True, "warning_code": None}
        )
        self.assertEqual(client.get(f"/api/jobs/{identifier}").status_code, 404)
        self.assertEqual(
            client.get("/api/jobs").json, {"jobs": [], "has_active_jobs": False}
        )
        worker.start.assert_not_called()

    def test_upload_limit_covers_the_pdf_and_the_multipart_envelope(self):
        settings = replace(self.settings, max_upload_mb=1)
        client, services = self.ready(settings=settings)
        for extra in (0, 65536):
            with (
                self.subTest(extra=extra),
                io.BytesIO(b"%PDF-" + b"x" * (1024 * 1024 + extra)) as pdf,
                client.post(
                    "/api/jobs",
                    data={"file": (pdf, "large.pdf")},
                ) as response,
            ):
                try:
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(
                        response.json,
                        {
                            "error_code": "upload_limit",
                            "error": "PDF too large (limit: 1 MiB)",
                        },
                    )
                    self.assertEqual(services.store.list_jobs()[0], [])
                    self.assertEqual(list(settings.data_dir.rglob(SOURCE_FILE)), [])
                finally:
                    # A 413 can leave the multipart body unread in its temp file.
                    response.request.input_stream.close()

    def test_live_cache_reasoning_and_disabled_preview_remain_per_application(self):
        client, services = self.ready()
        identifier = self.upload(client).json["id"]
        services.store.update_job(identifier, status="done")
        services.store.previews.put(
            identifier,
            {
                "content": "Final answer",
                "content_chars": 12,
                "reasoning": "PRIVATE_REASONING",
                "reasoning_chars": 17,
                "active": True,
            },
            12000,
        )
        response = client.get(f"/api/jobs/{identifier}/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["content"], "Final answer")
        self.assertFalse(response.json["active"])
        self.assertEqual(response.json["reasoning"], "")
        self.assertTrue(response.json["reasoning_available"])
        self.assertNotIn("_attempt_id", response.json)
        reasoned = client.get(f"/api/jobs/{identifier}/live?include_reasoning=true")
        self.assertEqual(reasoned.json["reasoning"], "PRIVATE_REASONING")
        self.assertEqual(
            services.store.previews.get(identifier)["reasoning"], "PRIVATE_REASONING"
        )
        disabled = create_app(
            replace(self.settings, live_output_enabled=False), services.store
        )
        hidden = disabled.test_client().get(
            f"/api/jobs/{identifier}/live?include_reasoning=true"
        )
        self.assertEqual(
            hidden.json,
            {
                "phase": "Queued",
                "content": "",
                "reasoning": "",
                "active": False,
                "enabled": False,
            },
        )

    def test_report_streaming_viewer_csp_i18n_and_symlink_protection(self):
        client, services = self.ready()
        identifier = self.upload(client).json["id"]
        self.assertEqual(client.get(f"/api/jobs/{identifier}/report").status_code, 409)
        services.store.update_job(identifier, status="done")
        path = services.store.job_dir(identifier) / "rapport_final.md"
        path.write_text(
            "# Résumé\n\n<script>PRIVATE_SCRIPT</script>\n\nTexte.", encoding="utf-8"
        )
        with client.get(f"/api/jobs/{identifier}/report") as download:
            self.assertEqual(download.status_code, 200)
            self.assertTrue(download.is_streamed)
            self.assertEqual(download.data, path.read_bytes())
            self.assertEqual(
                download.headers["Content-Type"], "text/markdown; charset=utf-8"
            )
            self.assertIn("filename*=UTF-8''", download.headers["Content-Disposition"])
        preview = client.get(f"/api/jobs/{identifier}/report/view?lang=fr")
        self.assertEqual(preview.status_code, 200)
        self.assertIn('<html lang="fr">', preview.get_data(as_text=True))
        self.assertNotIn(
            "<script>PRIVATE_SCRIPT</script>", preview.get_data(as_text=True)
        )
        self.assertIn(
            "&lt;script&gt;PRIVATE_SCRIPT&lt;/script&gt;",
            preview.get_data(as_text=True),
        )
        self.assertEqual(preview.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(
            preview.headers["Content-Security-Policy"],
            "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        path.unlink()
        outside = self.root / "private.md"
        outside.write_text("PRIVATE_OUTSIDE", encoding="utf-8")
        path.symlink_to(outside)
        self.assertEqual(client.get(f"/api/jobs/{identifier}/report").status_code, 404)
        self.assertEqual(
            client.get(f"/api/jobs/{identifier}/report/view").status_code, 404
        )

    def test_diagnostics_use_the_injected_factory_without_reading_the_prompt(self):
        backend = Mock(spec=BackendClient)
        backend.base_url = self.settings.llama_base_url
        backend.model_name = "private/model"
        backend.session = Mock(headers={})
        backend.inspect.return_value = {"status": "available", "context_tokens": 16384}
        factory = Mock(return_value=backend)
        client, services = self.ready(client_factory=factory)
        self.prompt.unlink()
        response = client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["backend"], {"status": "available", "context_tokens": 16384}
        )
        self.assertEqual(response.json["engine_settings"], "read_only")
        self.assertEqual(response.json["application"]["context_limit"], 16384)
        self.assertEqual(factory.call_args.args[0]["system_prompt"], "")
        self.assertEqual(client.get("/api/diagnostics").status_code, 200)
        factory.assert_called_once()
        backend.session.close.assert_called_once()
        self.assertEqual(services.capabilities.get()["context_tokens"], 16384)
        profiles = client.get("/api/profiles").json
        self.assertNotIn("engine_context_unknown", profiles["defaults"]["warnings"])

    def test_default_client_has_an_unambiguous_name_and_app_owned_cache(self):
        _, services = self.application()
        client = services.make_client()
        self.addCleanup(client.session.close)
        self.assertIsInstance(client, DeploymentModelClient)
        self.assertEqual(type(client).__name__, "DeploymentModelClient")
        self.assertIs(client._cache, services.capabilities)
        self.assertEqual(client.prompt, "PRIVATE_SYSTEM_PROMPT")

    def test_importing_factory_and_legacy_app_starts_no_network_or_worker(self):
        code = """
import pathlib
from unittest.mock import patch
with patch('document_processor.worker.Worker.start', side_effect=AssertionError('Worker started')), \
     patch('document_processor.jobs.JobStore.initialize', side_effect=AssertionError('Database initialized')), \
     patch('requests.Session', side_effect=AssertionError('HTTP session opened')), \
     patch('waitress.serve', side_effect=AssertionError('Server started')):
    import sys
    from document_processor.application import create_app
    assert 'app' not in sys.modules
    import app
    assert app.worker._thread is None
    assert not app.SETTINGS.data_dir.exists()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env={
                **os.environ,
                "DATA_DIR": str(self.root / "import-only"),
                "SYSTEM_PROMPT_FILE": str(self.prompt),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class LifecycleTests(unittest.TestCase):
    def test_sigterm_runs_worker_stop_before_sqlite_close_and_preserves_waitress_options(
        self,
    ):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        calls = []
        services = Mock()
        services.runtime_defaults.return_value = settings.job_defaults()
        services.store.initialize.side_effect = lambda: calls.append("initialize")
        services.worker.start.side_effect = lambda: calls.append("start")
        services.worker.stop.side_effect = lambda: calls.append("stop") or True
        services.store.close.side_effect = lambda: calls.append("close")
        application = Mock()
        with patch("document_processor.lifecycle.signal.signal") as handler:

            def serve(actual, **kwargs):
                self.assertIs(actual, application)
                self.assertEqual(
                    kwargs, {"host": "0.0.0.0", "port": 9932, "threads": 4}
                )
                calls.append("serve")
                handler.call_args.args[1](signal.SIGTERM, None)

            with self.assertRaises(SystemExit) as stopped:
                run_server(application, services, serve, settings)
        self.assertEqual(stopped.exception.code, 0)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0], signal.SIGTERM)
        self.assertEqual(calls, ["initialize", "start", "serve", "stop", "close"])

    def test_invalid_budgets_and_newer_schema_stop_before_starting_worker(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        services = Mock()
        services.runtime_defaults.return_value = replace(
            settings, context_tokens=1024
        ).job_defaults()
        server = Mock()
        with self.assertRaises(SystemExit) as invalid:
            run_server(Mock(), services, server, settings)
        self.assertEqual(
            str(invalid.exception),
            "Configuration error: Budgets exceed the available context; reduce them before starting.",
        )
        services.store.initialize.assert_not_called()
        services.runtime_defaults.return_value = settings.job_defaults()
        services.store.initialize.side_effect = SchemaVersionError("Newer schema")
        with self.assertRaises(SystemExit) as schema:
            run_server(Mock(), services, server, settings)
        self.assertEqual(str(schema.exception), "Database error: Newer schema")
        services.worker.start.assert_not_called()
        server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
