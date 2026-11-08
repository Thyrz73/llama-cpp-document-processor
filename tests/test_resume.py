"""Exercise configurable resume through HTTP, real SQLite and the worker pipeline."""

import copy
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from support import WordCountModelMixin

from document_processor import config, database, extraction, jobs
from document_processor.application import create_app
from document_processor.artifacts import Artifacts
from document_processor.errors import ModelError
from document_processor.jobs import JobStore
from document_processor.model_client import ModelClient
from document_processor.observability import log
from document_processor.settings import Settings


class ResumeModel(WordCountModelMixin, ModelClient):
    """Run actual orchestration without HTTP or inference, recording output budgets."""

    def inspect(self):
        self.capabilities = {
            "context_tokens": 16384,
            "model": "resume-model",
            "template_hash": config.fingerprint("template-1"),
            "version": "test",
            "sampling": {"temperature": 0.2},
        }
        return self.capabilities

    def model(self):
        return "resume-model"

    def complete(self, instruction, content, max_tokens, *, on_update=None):
        self.calls.append((self.current_stage, max_tokens))
        self.current_metrics = {"output_tokens": 12, "finish_reason": "stop"}
        if self.current_stage == self.fail_stage:
            raise ModelError("stream_error", "Simulated disconnect.")
        return "Measured value: 25 °C ± 0.5 °C [Page 1]. Limitation: one sample."


class ResumeApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="resume-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prompt = self.root / "prompt.txt"
        prompt.write_text("PRIVATE_SYSTEM_PROMPT", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = replace(
                Settings.from_env(),
                data_dir=self.root / "data",
                system_prompt_file=prompt,
                max_job_attempts=2,
            )
        self.store = JobStore(self.settings.data_dir)
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.application = create_app(self.settings, self.store)
        self.services = self.application.extensions["document_processor"]
        self.client = self.application.test_client()
        wake = patch.object(self.services.worker, "wake")
        self.wake = wake.start()
        self.addCleanup(wake.stop)

    def create(self, status="failed"):
        response = self.client.post(
            "/api/jobs",
            data={
                "file": (io.BytesIO(b"%PDF-test document"), "study.pdf"),
                "question": "Résumer les mesures.",
                "options": json.dumps(
                    {
                        "profile": "low",
                        "detail_level": "detailed",
                        "report_language": "auto",
                        "overrides": {"top_k": 40, "min_p": 0.05},
                    }
                ),
            },
        )
        self.assertEqual(response.status_code, 201, response.json)
        identifier = response.json["id"]
        self.store.update_job(
            identifier,
            status=status,
            cancel_requested=int(status == "cancelled"),
            error="Previous failure",
            error_code="stream_error",
        )
        self.wake.reset_mock()
        return identifier

    def snapshot(self):
        """Compare all durable rows to prove rejected requests have no side effects."""
        with self.store.connect() as con:
            return {
                table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
                for table in (
                    "jobs",
                    "job_configs",
                    "job_config_revisions",
                    "job_config_selections",
                    "job_runs",
                    "job_run_config_revisions",
                )
            }

    def post(self, identifier, **kwargs):
        return self.client.post(f"/api/jobs/{identifier}/resume", **kwargs)

    def rejected(self, identifier, status, code, **kwargs):
        before = self.snapshot()
        response = self.post(identifier, **kwargs)
        self.assertEqual(response.status_code, status, response.json)
        self.assertEqual(response.json["error_code"], code)
        self.assertEqual(self.snapshot(), before)
        self.wake.assert_not_called()
        return response

    def test_only_requested_budgets_change_and_public_views_follow_the_revision(self):
        for fields in (
            {"analysis_tokens"},
            {"merge_tokens"},
            {"final_tokens"},
            config.RESUME_BUDGETS,
        ):
            with self.subTest(fields=fields):
                identifier = self.create("cancelled")
                original = self.store.load_config(identifier)
                original_rows = self.snapshot()["job_configs"]
                self.client.get("/api/jobs")  # Prime the public configuration cache.
                increases = {name: original[name] + 128 for name in fields}
                response = self.post(identifier, json=increases)
                self.assertEqual(response.status_code, 200, response.json)
                expected = {**original, **increases}
                self.assertEqual(self.store.load_config(identifier), expected)
                self.assertEqual(
                    self.store.load_config(identifier, revision=0), original
                )
                self.assertEqual(self.snapshot()["job_configs"], original_rows)
                job = self.store.get_job(identifier)
                self.assertEqual(
                    (
                        job["status"],
                        job["cancel_requested"],
                        job["error"],
                        job["error_code"],
                    ),
                    ("queued", 0, "", ""),
                )
                self.assertEqual(job["question"], "Résumer les mesures.")
                self.assertEqual(self.store.attempt_count(identifier), 0)
                public = config.public_config(expected)
                self.assertEqual(
                    response.json,
                    {
                        "status": "queued",
                        "configuration": public,
                        "validation_warnings": ["engine_context_unknown"],
                    },
                )
                self.assertEqual(
                    self.client.get(f"/api/jobs/{identifier}").json["configuration"],
                    public,
                )
                listed = self.client.get("/api/jobs").json["jobs"]
                self.assertEqual(
                    next(job for job in listed if job["id"] == identifier)[
                        "configuration"
                    ],
                    public,
                )
                for private in ("PRIVATE_SYSTEM_PROMPT", "templates", "source_hash"):
                    self.assertNotIn(private, response.get_data(as_text=True))
                self.wake.assert_called_once_with()

    def test_changed_deployment_defaults_and_prompt_files_are_never_resolved(self):
        identifier = self.create()
        saved = self.store.load_config(identifier)
        self.store.start_attempt(identifier, jobs.utcnow()).finish("failed")
        changed = replace(
            self.settings,
            context_tokens=512,
            analysis_tokens=64,
            merge_tokens=64,
            final_tokens=64,
            output_language="en",
            max_job_attempts=1,
            thinking_adapter="prompt",
        )
        self.settings.system_prompt_file.unlink()
        application = create_app(changed, self.store)
        services = application.extensions["document_processor"]
        with (
            patch.object(
                services, "new_snapshot", side_effect=AssertionError("New defaults")
            ),
            patch.object(
                services, "read_system_prompt", side_effect=AssertionError("New prompt")
            ),
            patch.object(services.worker, "wake") as wake,
        ):
            response = application.test_client().post(
                f"/api/jobs/{identifier}/resume", json={"merge_tokens": 4096}
            )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            self.store.load_config(identifier), {**saved, "merge_tokens": 4096}
        )
        wake.assert_called_once_with()

    def test_expired_engine_context_warning_does_not_change_snapshot_or_artifacts(self):
        self.services.capabilities.remember(
            {"status": "available", "context_tokens": 8192}
        )
        identifier = self.create()
        saved = self.store.load_config(identifier)
        self.assertNotIn("engine_context_unknown", saved["warnings"])
        before = self.snapshot()
        checksum = config.fingerprint(saved)
        artifacts = Artifacts(self.store.job_dir(identifier), saved)
        dependencies = {"source": "test"}
        artifacts.write("notes.md", "PRIVATE_ARTIFACT", dependencies)
        # Expire deterministically, without sleeping or changing the saved snapshot.
        self.services.capabilities.entry["expires"] = 0
        self.assertIsNone(self.services.capabilities.get())
        response = self.post(identifier, json={})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            response.json["validation_warnings"], ["engine_context_unknown"]
        )
        self.assertEqual(response.json["configuration"], config.public_config(saved))
        self.assertEqual(response.json["configuration"]["fingerprint"], checksum)
        self.assertEqual(self.store.load_config(identifier), saved)
        after = self.snapshot()
        for table in before.keys() - {"jobs"}:
            self.assertEqual(after[table], before[table], table)
        resumed_artifacts = Artifacts(
            self.store.job_dir(identifier), self.store.load_config(identifier)
        )
        self.assertEqual(resumed_artifacts.root, artifacts.root)
        self.assertEqual(
            resumed_artifacts.read("notes.md", dependencies), "PRIVATE_ARTIFACT"
        )
        body = response.get_data(as_text=True)
        for private in (
            "PRIVATE_ARTIFACT",
            "PRIVATE_SYSTEM_PROMPT",
            "source_hash",
            "templates",
        ):
            self.assertNotIn(private, body)
        # Unknown engine context still enforces the immutable application ceiling.
        self.store.update_job(identifier, status="failed")
        self.wake.reset_mock()
        self.rejected(
            identifier,
            400,
            "context_insufficient",
            json={"final_tokens": saved["context_tokens"]},
        )

    def test_current_context_warnings_do_not_change_budget_revision_identity(self):
        observation = {"status": "available", "context_tokens": 8192}
        self.services.capabilities.remember(observation)
        identifier = self.create()
        saved = self.store.load_config(identifier)
        original_rows = self.snapshot()["job_configs"]
        expected = {**saved, "merge_tokens": 4096}
        self.services.capabilities.entry["expires"] = 0
        unknown = self.post(identifier, json={"merge_tokens": 4096})
        self.assertEqual(unknown.status_code, 200, unknown.json)
        self.assertEqual(
            unknown.json["validation_warnings"], ["engine_context_unknown"]
        )
        self.assertEqual(self.store.load_config(identifier), expected)
        self.assertEqual(
            unknown.json["configuration"]["fingerprint"], config.fingerprint(expected)
        )
        revisions = self.snapshot()["job_config_revisions"]
        self.assertEqual(len(revisions), 1)
        self.store.update_job(identifier, status="failed")
        self.services.capabilities.remember(observation)
        known = self.post(identifier, json={"merge_tokens": 4096})
        self.assertEqual(known.status_code, 200, known.json)
        self.assertEqual(known.json["validation_warnings"], [])
        self.assertEqual(known.json["configuration"], unknown.json["configuration"])
        self.assertEqual(self.snapshot()["job_config_revisions"], revisions)
        self.assertEqual(self.snapshot()["job_configs"], original_rows)
        self.assertEqual(self.store.load_config(identifier, revision=0), saved)
        self.assertNotIn("validation_warnings", self.store.load_config(identifier))

    def test_historical_unknown_context_warning_is_separate_from_current_known_context(
        self,
    ):
        identifier = self.create()
        saved = self.store.load_config(identifier)
        self.assertIn("engine_context_unknown", saved["warnings"])
        self.services.capabilities.remember(
            {"status": "available", "context_tokens": 8192}
        )
        response = self.post(identifier)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["validation_warnings"], [])
        self.assertEqual(response.json["configuration"]["warnings"], saved["warnings"])
        self.assertEqual(self.store.load_config(identifier), saved)
        self.assertEqual(self.snapshot()["job_config_revisions"], [])

    def test_invalid_types_and_ranges_are_rejected_without_coercion(self):
        identifier = self.create()
        for name in config.RESUME_BUDGETS:
            for value in (
                True,
                False,
                4096.0,
                "4096",
                None,
                [],
                {},
                -1,
                0,
                63,
                65537,
                10**40,
            ):
                with self.subTest(name=name, value=value):
                    self.rejected(identifier, 400, "invalid_budget", json={name: value})

    def test_unsupported_keys_cannot_change_other_settings(self):
        identifier = self.create()
        for name in (
            "chunk_tokens",
            "context_tokens",
            "profile",
            "detail_level",
            "overrides",
            "max_attempts",
            "max_calls",
            "temperature",
            "thinking_final",
            "system_prompt",
            "templates",
            "source_hash",
            "question",
            "config_revision",
            "report_language",
        ):
            with self.subTest(name=name):
                self.rejected(identifier, 400, "invalid_overrides", json={name: 4096})

    def test_decreases_are_compared_to_the_current_revision(self):
        identifier = self.create()
        original = self.store.load_config(identifier)
        increases = {name: original[name] + 128 for name in config.RESUME_BUDGETS}
        self.assertEqual(self.post(identifier, json=increases).status_code, 200)
        self.store.update_job(identifier, status="failed")
        self.wake.reset_mock()
        for name in config.RESUME_BUDGETS:
            with self.subTest(name=name):
                self.rejected(
                    identifier, 400, "invalid_budget", json={name: original[name] + 64}
                )
        response = self.post(
            identifier, json={"merge_tokens": increases["merge_tokens"] + 64}
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            self.store.load_config(identifier),
            {**original, **increases, "merge_tokens": increases["merge_tokens"] + 64},
        )

    def test_malformed_non_object_non_json_and_oversized_bodies_are_rejected(self):
        identifier = self.create()
        for data, content_type in (
            ("null", "application/json"),
            ("[]", "application/json"),
            ("true", "application/json"),
            ("4096", "application/json"),
            ('"4096"', "application/json"),
            ('{"merge_tokens":', "application/json"),
            ("{}", "text/plain"),
            ("merge_tokens=4096", "application/x-www-form-urlencoded"),
            (" " * 8193, "application/json"),
        ):
            with self.subTest(data=data[:40], content_type=content_type):
                self.rejected(
                    identifier,
                    400,
                    "invalid_options",
                    data=data,
                    content_type=content_type,
                )

    def test_empty_or_unchanged_budgets_reuse_the_saved_snapshot(self):
        identifier = self.create()
        saved = self.store.load_config(identifier)
        for kwargs in (
            {},
            {"json": {}},
            {"json": {"merge_tokens": saved["merge_tokens"]}},
        ):
            with self.subTest(kwargs=kwargs):
                self.store.update_job(identifier, status="failed")
                response = self.post(identifier, **kwargs)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(self.store.load_config(identifier), saved)
                self.assertEqual(self.snapshot()["job_config_revisions"], [])
                self.assertEqual(self.snapshot()["job_config_selections"], [])
        self.assertEqual(self.store.attempt_count(identifier), 0)

    def test_context_boundaries_match_creation_for_saved_and_observed_limits(self):
        for engine_context in (None, 8192):
            capabilities = (
                {} if engine_context is None else {"context_tokens": engine_context}
            )
            self.services.capabilities.remember(capabilities)
            for name in config.RESUME_BUDGETS:
                for excess in (0, 1):
                    with self.subTest(
                        engine_context=engine_context, name=name, excess=excess
                    ):
                        identifier = self.create()
                        saved = self.store.load_config(identifier)
                        context = engine_context or saved["context_tokens"]
                        maximum = context - (
                            saved["chunk_tokens"] + 768
                            if name == "analysis_tokens"
                            else 769
                        )
                        overrides = {name: maximum + excess}
                        if excess:
                            with self.assertRaises(config.ConfigurationError) as caught:
                                config.resolve(
                                    saved, {"overrides": overrides}, capabilities
                                )
                            self.assertEqual(
                                caught.exception.code, "context_insufficient"
                            )
                            self.rejected(
                                identifier, 400, "context_insufficient", json=overrides
                            )
                        else:
                            config.resolve(
                                saved, {"overrides": overrides}, capabilities
                            )
                            response = self.post(identifier, json=overrides)
                            self.assertEqual(response.status_code, 200, response.json)
                            self.assertEqual(
                                self.store.load_config(identifier)[name], maximum
                            )

    def test_only_failed_or_cancelled_states_can_resume(self):
        for status in ("queued", "running", "done"):
            with self.subTest(status=status):
                identifier = self.create(status)
                self.rejected(
                    identifier, 409, "resume_conflict", json={"merge_tokens": 4096}
                )

    def test_finishing_attempt_must_complete_before_resume(self):
        identifier = self.create()
        recorder = self.store.start_attempt(identifier, jobs.utcnow())
        self.rejected(identifier, 409, "resume_conflict", json={"merge_tokens": 4096})
        recorder.finish("failed")
        self.assertEqual(
            self.post(identifier, json={"merge_tokens": 4096}).status_code, 200
        )

    def test_unknown_jobs_return_404_without_waking_the_worker(self):
        for identifier in ("a" * 32, "invalid-id"):
            with self.subTest(identifier=identifier):
                self.rejected(identifier, 404, "http_404", json={"merge_tokens": 4096})

    def test_saved_attempt_limits_include_interrupted_and_cancelled_runs(self):
        identifier = self.create()
        for status in ("interrupted", "cancelled"):
            self.store.start_attempt(identifier, jobs.utcnow()).finish(status)
        application = create_app(
            replace(self.settings, max_job_attempts=100), self.store
        )
        services = application.extensions["document_processor"]
        with patch.object(services.worker, "wake") as wake:
            before = self.snapshot()
            response = application.test_client().post(
                f"/api/jobs/{identifier}/resume", json={"merge_tokens": 4096}
            )
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(response.json["error_code"], "retry_limit")
            self.assertEqual(self.snapshot(), before)
            wake.assert_not_called()
        self.assertEqual(
            self.client.post(f"/api/jobs/{identifier}/retry").json["error_code"],
            "retry_limit",
        )

    def test_commit_precedes_worker_wakeup(self):
        identifier = self.create()

        def wake():
            # A separate connection sees only committed data, like a worker thread.
            with database.connect(self.store.db_path) as con:
                self.assertEqual(
                    con.execute(
                        "SELECT status FROM jobs WHERE id=?", (identifier,)
                    ).fetchone()[0],
                    "queued",
                )
                self.assertEqual(
                    con.execute(
                        "SELECT revision FROM job_config_selections WHERE job_id=?",
                        (identifier,),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM job_config_revisions WHERE job_id=?",
                        (identifier,),
                    ).fetchone()[0],
                    1,
                )

        self.wake.side_effect = wake
        self.assertEqual(
            self.post(identifier, json={"merge_tokens": 4096}).status_code, 200
        )
        self.wake.assert_called_once_with()

    def test_queue_revision_and_selection_roll_back_on_any_write_failure(self):
        identifier = self.create()
        self.assertEqual(
            self.post(identifier, json={"analysis_tokens": 4096}).status_code, 200
        )
        self.store.update_job(identifier, status="failed")
        self.wake.reset_mock()
        self.store.list_jobs()
        for action in (
            "UPDATE ON jobs",
            "INSERT ON job_config_revisions",
            "INSERT ON job_config_selections",
        ):
            with self.subTest(action=action):
                with self.store.connect() as con:
                    con.execute(
                        f"CREATE TRIGGER fail_resume BEFORE {action} BEGIN SELECT RAISE(ABORT, 'PRIVATE_FAILURE'); END"
                    )
                with self.assertLogs(log, "ERROR"):
                    response = self.rejected(
                        identifier, 500, "http_500", json={"merge_tokens": 4096}
                    )
                self.assertNotIn("PRIVATE_FAILURE", response.get_data(as_text=True))
                self.assertTrue(self.store._public_configs)
                with self.store.connect() as con:
                    con.execute("DROP TRIGGER fail_resume")

    def test_corrupt_snapshot_is_never_queued_or_replaced_by_defaults(self):
        identifier = self.create()
        with self.store.connect() as con:
            con.execute(
                "UPDATE job_configs SET snapshot='PRIVATE_CORRUPT' WHERE job_id=?",
                (identifier,),
            )
        with self.assertLogs(log, "ERROR"):
            response = self.rejected(
                identifier, 500, "http_500", json={"merge_tokens": 4096}
            )
        self.assertNotIn("PRIVATE_CORRUPT", response.get_data(as_text=True))

    def test_simultaneous_resumes_create_one_revision_and_one_queued_attempt(self):
        for distinct in (False, True):
            with self.subTest(distinct=distinct):
                identifier = self.create()
                barrier = threading.Barrier(6)

                def resume(
                    index, *, barrier=barrier, identifier=identifier, distinct=distinct
                ):
                    try:
                        with self.application.test_client() as client:
                            barrier.wait(timeout=5)
                            response = client.post(
                                f"/api/jobs/{identifier}/resume",
                                json={
                                    "merge_tokens": 4096 + (index if distinct else 0)
                                },
                            )
                            return response.status_code, response.json
                    finally:
                        self.store.close(checkpoint=False)

                with ThreadPoolExecutor(max_workers=6) as executor:
                    responses = list(executor.map(resume, range(6)))
                self.assertEqual(
                    sorted(status for status, _ in responses),
                    [200, 409, 409, 409, 409, 409],
                )
                winner = next(body for status, body in responses if status == 200)
                self.assertEqual(
                    config.public_config(self.store.load_config(identifier)),
                    winner["configuration"],
                )
                with self.store.connect() as con:
                    self.assertEqual(
                        con.execute(
                            "SELECT COUNT(*) FROM job_config_revisions WHERE job_id=?",
                            (identifier,),
                        ).fetchone()[0],
                        1,
                    )
                self.wake.assert_called_once_with()
                self.assertEqual(self.store.claim_next_job(), identifier)
                self.assertIsNone(self.store.claim_next_job())
                self.store.start_attempt(identifier, jobs.utcnow()).finish("failed")
                self.assertEqual(self.store.attempt_count(identifier), 1)
                self.store.update_job(identifier, status="failed")

    def test_retry_and_resume_compete_for_the_same_queue_transition(self):
        identifier = self.create()
        original = self.store.load_config(identifier)
        barrier = threading.Barrier(2)

        def request(action):
            try:
                with self.application.test_client() as client:
                    barrier.wait(timeout=5)
                    kwargs = (
                        {"json": {"merge_tokens": 4096}} if action == "resume" else {}
                    )
                    response = client.post(f"/api/jobs/{identifier}/{action}", **kwargs)
                    return action, response.status_code
            finally:
                self.store.close(checkpoint=False)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(request, ("retry", "resume")))
        self.assertEqual(sorted(status for _, status in responses), [200, 409])
        winner = next(action for action, status in responses if status == 200)
        expected = (
            {**original, "merge_tokens": 4096} if winner == "resume" else original
        )
        self.assertEqual(self.store.load_config(identifier), expected)
        self.wake.assert_called_once_with()

    def test_retry_keeps_selected_revision_and_rejects_all_request_bodies(self):
        identifier = self.create()
        self.assertEqual(
            self.post(identifier, json={"merge_tokens": 4096}).status_code, 200
        )
        selected = self.store.load_config(identifier)
        self.store.update_job(identifier, status="failed")
        self.wake.reset_mock()
        for payload in ({}, {"merge_tokens": 8192}):
            response = self.client.post(f"/api/jobs/{identifier}/retry", json=payload)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(response.json["error_code"], "immutable_config")
        self.wake.assert_not_called()
        response = self.client.post(f"/api/jobs/{identifier}/retry")
        self.assertEqual(response.json, {"status": "queued", "warnings": []})
        self.assertEqual(self.store.load_config(identifier), selected)

    def test_legacy_without_snapshot_requires_retry_and_retains_legacy_warnings(self):
        identifier = self.create()
        with self.store.connect() as con:
            con.execute("DELETE FROM job_configs WHERE job_id=?", (identifier,))
        self.rejected(
            identifier, 409, "resume_config_missing", json={"merge_tokens": 4096}
        )
        self.rejected(identifier, 409, "resume_config_missing")
        response = self.client.post(f"/api/jobs/{identifier}/retry")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            response.json, {"status": "queued", "warnings": ["legacy_recomputed"]}
        )
        self.assertIsNone(self.store.load_config(identifier))

    def test_worker_uses_resumed_budgets_after_restart_and_pins_both_attempts(self):
        identifier = self.create("queued")
        original = self.store.load_config(identifier)
        models = []

        def factory(snapshot):
            model = ResumeModel(
                "http://unused", "", snapshot, snapshot["system_prompt"]
            )
            model.calls = []
            model.fail_stage = "final" if not models else None
            models.append(model)
            return model

        chunks = [
            {"text": "Sample measurements [Page 1]", "pages": [1]} for _ in range(5)
        ]
        with (
            patch.object(
                extraction,
                "extract_pdf",
                return_value=([{"page": 1, "text": "Measurements"}], 1, [], []),
            ),
            patch.object(extraction, "make_chunks", return_value=chunks),
        ):
            application = create_app(self.settings, self.store, factory)
            services = application.extensions["document_processor"]
            self.assertEqual(self.store.claim_next_job(), identifier)
            with self.assertLogs(log, "ERROR"):
                services.worker.run_claimed(identifier)
            self.assertEqual(self.store.get_job(identifier)["status"], "failed")
            increases = {
                "analysis_tokens": 4096,
                "merge_tokens": 4096,
                "final_tokens": 6144,
            }
            with patch.object(services.worker, "wake"):
                response = application.test_client().post(
                    f"/api/jobs/{identifier}/resume", json=increases
                )
            self.assertEqual(response.status_code, 200, response.json)
            expected = {**original, **increases}
            self.store.close()
            restarted = JobStore(self.store.data_dir)
            self.addCleanup(restarted.close)
            restarted.initialize()
            changed = replace(
                self.settings,
                analysis_tokens=64,
                merge_tokens=64,
                final_tokens=64,
                max_job_attempts=1,
            )
            application = create_app(changed, restarted, factory)
            services = application.extensions["document_processor"]
            self.assertEqual(restarted.claim_next_job(), identifier)
            services.worker.run_claimed(identifier)
        self.assertEqual(restarted.get_job(identifier)["status"], "done")
        self.assertEqual(models[1].config, expected)
        self.assertEqual(models[1].calls, [("final", 6144)])
        metrics = restarted.metrics(identifier)
        self.assertEqual([run["status"] for run in metrics["runs"]], ["done", "failed"])
        for run, expected_config in zip(
            metrics["runs"], (expected, original), strict=True
        ):
            self.assertEqual(
                restarted.load_attempt_config(identifier, run["id"]), expected_config
            )
        self.assertEqual(restarted.load_config(identifier, revision=0), original)
        reused = [
            step
            for step in metrics["steps"]
            if step["run_id"] == metrics["runs"][0]["id"] and step["status"] == "reused"
        ]
        self.assertEqual(
            {step["step_key"] for step in reused},
            {"extracted.json", "chunks.json", "merge_00_0000.md"}
            | {f"analysis_{index:04d}.md" for index in range(5)},
        )
        for step in reused:
            self.assertEqual(step["metrics"]["artifact_source_revision"], 0)
            if step["stage"] in ("section", "merge"):
                key = (
                    "analysis_tokens" if step["stage"] == "section" else "merge_tokens"
                )
                self.assertEqual(step["metrics"]["output_budget"], original[key])

    def test_historical_snapshot_metadata_and_extensions_are_preserved(self):
        identifier = self.create()
        saved = copy.deepcopy(self.store.load_config(identifier))
        saved.update(
            profile="old-profile",
            profile_version=0,
            historical_extension={"list": [None, 1]},
        )
        revision = self.store.save_config_revision(identifier, saved)
        self.store.requeue(identifier, 2, config_revision=revision)
        self.store.update_job(identifier, status="failed")
        response = self.post(identifier, json={"merge_tokens": 4096})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            self.store.load_config(identifier), {**saved, "merge_tokens": 4096}
        )


if __name__ == "__main__":
    unittest.main()
