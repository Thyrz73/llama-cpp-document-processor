"""Exercise V2 contracts with temporary storage and a deterministic fake model."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import config, jobs, pipeline
from document_processor.errors import ProcessingError
from document_processor.model_client import ModelError, Cancelled


class FakeModel(app.ModelClient):
    """Emulate a stable model without any HTTP request or language inference."""

    calls = []
    identity = "test-model"
    fail_stage = None

    def inspect(self):
        self.capabilities = {"context_tokens": 8192, "model": self.identity,
                             "template_hash": "template-1", "version": "test"}
        return self.capabilities

    def model(self):
        return self.identity

    def tokenize(self, text):
        self.tokenization_calls += 1
        return len(text.split())

    def complete(self, instruction, content, max_tokens, *, on_update=None):
        self.calls.append((self.current_stage, self.config, instruction, max_tokens))
        self.current_metrics = {"output_tokens": 12, "finish_reason": "stop", "output_budget": max_tokens}
        if self.current_stage == self.fail_stage:
            self.current_metrics.update(error_code="stream_error", finish_reason=None)
            raise ModelError("stream_error", "Simulated disconnect.")
        text = "Measured value: 25 °C ± 0.5 °C [Page 1]. Limitation: one sample."
        if on_update:
            on_update(text, "emitted reasoning", True)
        return text


class V2Tests(unittest.TestCase):
    """Keep all jobs, snapshots and PDFs inside a disposable test directory."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (("DATA", self.root), ("DB_PATH", self.root / "jobs.sqlite3")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(app, "read_system_prompt", return_value="Private instructions: KEEP_PRIVATE")
        self.prompt = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(app, "_capability_cache", {"value": None, "expires": 0})
        patcher.start()
        self.addCleanup(patcher.stop)
        app.init_db()
        self.client = app.app.test_client()
        FakeModel.calls, FakeModel.identity, FakeModel.fail_stage = [], "test-model", None

    def create(self, options=None):
        """Use the actual historical multipart endpoint, adding options only when supplied."""
        data = {"file": (io.BytesIO(b"%PDF-test document"), "example.pdf"), "question": "Summarize"}
        if options is not None:
            data["options"] = json.dumps(options)
        response = self.client.post("/api/jobs", data=data)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def run_job(self, identifier, count=5):
        """Drive real orchestration with extraction/model substitutes and real persistence."""
        app.update_job(identifier, status="running")
        chunks = [{"text": "Sample measurements [Page 1]", "pages": [1]} for _ in range(count)]
        with patch.object(app, "ModelClient", FakeModel), \
             patch.object(app, "extract_pdf", return_value=([{"page": 1, "text": "Measurements"}], 1, [], [])), \
             patch.object(app, "make_chunks", return_value=chunks):
            app.analyze_job(identifier)

    def test_profiles_are_distinct_and_do_not_change_detail_or_concurrency(self):
        values = []
        for name in ("low", "medium", "high", "ultra"):
            resolved = config.resolve(app.runtime_defaults(), {"profile": name})
            values.append((resolved["chunk_tokens"], resolved["analysis_tokens"], resolved["merge_tokens"]))
            self.assertEqual((resolved["final_tokens"], resolved["concurrency"]),
                             ({"low": 3072, "medium": 4096, "high": 6144, "ultra": 8192}[name], 1))
            self.assertIn("profile_uncalibrated", resolved["warnings"])
        self.assertEqual(len(set(values)), 4)

    def test_low_profile_uses_reference_budgets(self):
        resolved = config.resolve(app.runtime_defaults(), {"profile": "low"})
        self.assertEqual(
            (resolved["chunk_tokens"], resolved["analysis_tokens"],
             resolved["merge_tokens"], resolved["final_tokens"]),
            (3000, 3072, 2048, 3072),
        )

    def test_debug_logs_stay_bounded_during_frequent_stream_updates(self):
        identifier = self.create()
        original = FakeModel.complete

        def streaming(model, instruction, content, max_tokens, *, on_update=None):
            for _ in range(50):
                on_update("PRIVATE_STREAM_CONTENT", "PRIVATE_REASONING", False)
            return original(model, instruction, content, max_tokens, on_update=on_update)

        with patch.object(FakeModel, "complete", streaming), self.assertLogs(app.log, "DEBUG") as captured:
            self.run_job(identifier, 1)
        output = "\n".join(captured.output)
        for private in ("PRIVATE_STREAM_CONTENT", "PRIVATE_REASONING", "KEEP_PRIVATE"):
            self.assertNotIn(private, output)
        self.assertLess(len(captured.records), 45)
        self.assertEqual(len(FakeModel.calls), 2, "Naming and logs must not add generation calls")
        self.assertEqual(sum("step_started" in line for line in captured.output), 5)

    def test_legacy_options_keep_environment_defaults(self):
        with patch.object(app, "ANALYSIS_TOKENS", 1500):
            identifier = self.create()
        saved = jobs.load_config(app.db_conn, identifier)
        self.assertEqual((saved["profile"], saved["analysis_tokens"]), ("legacy", 1500))

    def test_profile_and_overrides_are_validated_without_network(self):
        cases = [{"profile": "unknown"}, {"overrides": {"base_url": "http://invalid"}},
                 {"overrides": {"analysis_tokens": -1}}, {"overrides": {"analysis_tokens": True}},
                 {"overrides": {"temperature": float("nan")}}, {"overrides": {"top_k": 2.5}},
                 {"report_language": "arbitrary"}, {"overrides": {"thinking_section": "off"}},
                 {"overrides": {"chunk_tokens": 100000}}, {"profile": []}]
        with patch("requests.Session.request", side_effect=AssertionError("Network forbidden")):
            for options in cases:
                with self.subTest(options=options):
                    self.assertEqual(self.client.post("/api/config/preview", json=options).status_code, 400)
            response = self.client.post("/api/config/preview", json={"profile": "medium", "overrides": {"analysis_tokens": 1200}})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["configuration"]["customized"])
            self.assertNotIn("KEEP_PRIVATE", response.get_data(as_text=True))

    def test_known_context_rejects_but_unknown_context_warns(self):
        with self.assertRaises(config.ConfigurationError):
            config.resolve(app.runtime_defaults(), {"profile": "low"}, {"context_tokens": 2048})
        value = config.resolve(app.runtime_defaults(), {"profile": "low"}, {})
        self.assertIn("engine_context_unknown", value["warnings"])

    def test_malformed_options_and_input_limits(self):
        for raw in ("[", "[]", "null", '"text"', "x" * 8200):
            with self.subTest(raw=raw[:10]):
                response = self.client.post("/api/jobs", data={"options": raw})
                self.assertEqual(response.status_code, 400)
        for data in ({}, {"file": (io.BytesIO(b"other"), "a.pdf")},
                     {"file": (io.BytesIO(b"%PDF-test"), "a.txt")},
                     {"file": (io.BytesIO(b"%PDF-test"), "a.pdf"), "question": "q" * 1501}):
            self.assertEqual(self.client.post("/api/jobs", data=data).status_code, 400)
        with patch.dict(app.app.config, MAX_CONTENT_LENGTH=10):
            self.assertEqual(self.client.post("/api/jobs", data=b"x" * 100).status_code, 413)

    def test_snapshot_stays_private_and_survives_environment_and_prompt_changes(self):
        identifier = self.create({"profile": "low", "report_language": "fr"})
        saved = jobs.load_config(app.db_conn, identifier)
        self.prompt.return_value = "CHANGED PROMPT"
        with patch.object(app, "ANALYSIS_TOKENS", 99), patch.object(app, "OUTPUT_LANGUAGE", "English"):
            self.run_job(identifier)
        self.assertEqual(jobs.load_config(app.db_conn, identifier), saved)
        self.assertTrue(all(call[1]["system_prompt"] == "Private instructions: KEEP_PRIVATE" for call in FakeModel.calls))
        for route in ("/api/jobs", f"/api/jobs/{identifier}", f"/api/jobs/{identifier}/metrics"):
            response = self.client.get(route)
            self.assertNotIn("KEEP_PRIVATE", response.get_data(as_text=True))
        report = (app.job_dir(identifier) / "rapport_final.md").read_text()
        self.assertIn("# Analyse de", report)
        self.assertIn("25 °C ± 0.5 °C [Page 1]", report)
        live = self.client.get(f"/api/jobs/{identifier}/live").json
        self.assertIn("25 °C", live["content"])
        self.assertFalse(live["active"])

    def test_retry_reuses_only_verified_steps_and_records_failures(self):
        identifier = self.create({"profile": "low"})
        FakeModel.fail_stage = "final"
        with self.assertRaises(ModelError):
            self.run_job(identifier)
        self.assertEqual(len(FakeModel.calls), 7)
        app.update_job(identifier, status="failed")
        FakeModel.fail_stage = None
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/retry").status_code, 200)
        self.run_job(identifier)
        self.assertEqual(len(FakeModel.calls), 8)
        payload = self.client.get(f"/api/jobs/{identifier}/metrics").json
        self.assertEqual(len(payload["runs"]), 2)
        self.assertGreater(payload["summary"]["reused_steps"], 5)
        self.assertEqual(payload["summary"]["failed_steps"], 1)
        with self.client.get(f"/api/jobs/{identifier}/report") as response:
            self.assertEqual(response.status_code, 200)

    def test_tampered_artifact_is_recomputed(self):
        identifier = self.create()
        self.run_job(identifier, 1)
        first_calls = len(FakeModel.calls)
        note = next(app.job_dir(identifier).glob("artifacts/*/analysis_0000.md"))
        note.write_text("Tampered note", encoding="utf-8")
        self.run_job(identifier, 1)
        self.assertEqual(len(FakeModel.calls), first_calls + 1)
        self.assertNotIn("Tampered", note.read_text())

    def test_unpublished_or_incomplete_manifest_entry_is_not_reused(self):
        identifier = self.create()
        self.run_job(identifier, 1)
        manifest_path = next(app.job_dir(identifier).glob("artifacts/*/manifest.json"))
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"]["analysis_0000.md"]["complete"] = False
        manifest_path.write_text(json.dumps(manifest))
        previous = len(FakeModel.calls)
        self.run_job(identifier, 1)
        self.assertEqual(len(FakeModel.calls), previous + 1)

    def test_eight_sections_use_eleven_generations_and_all_metrics_are_paginated(self):
        identifier = self.create()
        self.run_job(identifier, 8)
        self.assertEqual(len(FakeModel.calls), 11)
        first = self.client.get(f"/api/jobs/{identifier}/metrics?limit=2").json
        self.assertEqual(len(first["steps"]), 2)
        second = self.client.get(f"/api/jobs/{identifier}/metrics?limit=2&before={first['next_before']}").json
        self.assertTrue(set(step["id"] for step in first["steps"]).isdisjoint(step["id"] for step in second["steps"]))
        self.assertEqual(self.client.get(f"/api/jobs/{identifier}/metrics?limit=9999").status_code, 400)

    def test_model_or_source_changes_cannot_silently_reuse_artifacts(self):
        identifier = self.create()
        self.run_job(identifier, 1)
        FakeModel.identity = "different-model"
        with self.assertRaisesRegex(ProcessingError, "model or template changed") as raised:
            self.run_job(identifier, 1)
        self.assertEqual(raised.exception.code, "identity_changed")
        FakeModel.identity = "test-model"
        (app.job_dir(identifier) / "source.pdf").write_bytes(b"changed")
        with self.assertRaisesRegex(ProcessingError, "source PDF changed") as raised:
            self.run_job(identifier, 1)
        self.assertEqual(raised.exception.code, "source_changed")

    def test_legacy_files_remain_intact_and_old_report_is_archived(self):
        identifier = self.create()
        with app.db_conn() as con:
            con.execute("DELETE FROM job_configs WHERE job_id=?", (identifier,))
        directory = app.job_dir(identifier)
        (directory / "analysis_0000.md").write_text("Historical notes")
        (directory / "rapport_final.md").write_text("Historical report")
        self.run_job(identifier, 1)
        self.assertEqual((directory / "analysis_0000.md").read_text(), "Historical notes")
        self.assertEqual(next(directory.glob("previous_reports/*.md")).read_text(), "Historical report")
        self.assertIn("legacy_recomputed", jobs.load_config(app.db_conn, identifier)["warnings"])

    def test_migration_is_idempotent_and_preserves_finished_jobs(self):
        identifier = self.create()
        app.update_job(identifier, status="done")
        before = app.get_job(identifier)
        app.init_db()
        app.init_db()
        self.assertEqual(app.get_job(identifier), before)
        with app.db_conn() as con:
            self.assertEqual([row[0] for row in con.execute("SELECT version FROM schema_migrations ORDER BY version")], [2, 3])

    def test_running_job_cancellation_is_signalled_without_polling_sqlite(self):
        identifier = self.create()
        app.update_job(identifier, status="running")
        observed = []

        def analyze(job_id):
            with patch.object(app, "read_cancellation", wraps=app.read_cancellation) as reads:
                checks = [app.cancellation_requested(job_id) for _ in range(500)]
                observed.append((checks[-1], reads.call_count))
                self.assertEqual(self.client.post(f"/api/jobs/{job_id}/cancel").status_code, 202)
                observed.append((app.cancellation_requested(job_id), reads.call_count))
            raise Cancelled()

        # A long interval keeps the read counts independent of machine speed.
        with patch.object(app, "analyze_job", side_effect=analyze), \
             patch.object(app, "CANCEL_RECHECK_SECONDS", 3600):
            app.run_claimed_job(identifier)
        # 500 checks open SQLite once; the API request is then seen without a new read.
        self.assertEqual(observed, [(False, 1), (True, 1)])
        self.assertEqual(app.get_job(identifier)["status"], "cancelled")
        self.assertNotIn(identifier, app._cancel_signals)

    def test_running_job_still_honors_cancellation_recorded_only_in_sqlite(self):
        identifier = self.create()
        app.update_job(identifier, status="running")

        def analyze(job_id):
            self.assertFalse(app.cancellation_requested(job_id))
            app.update_job(job_id, cancel_requested=1)
            self.assertFalse(app.cancellation_requested(job_id))
            app._cancel_signals[job_id].checked_at -= app.CANCEL_RECHECK_SECONDS
            self.assertTrue(app.cancellation_requested(job_id))
            raise Cancelled()

        with patch.object(app, "analyze_job", side_effect=analyze), \
             patch.object(app, "CANCEL_RECHECK_SECONDS", 3600):
            app.run_claimed_job(identifier)
        self.assertEqual(app.get_job(identifier)["status"], "cancelled")

    def test_cancel_and_retry_are_idempotent_and_configuration_cannot_be_replaced(self):
        identifier = self.create()
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/cancel").status_code, 202)
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/cancel").status_code, 200)
        self.assertEqual(app.get_job(identifier)["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/retry", json={"profile": "ultra"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/retry").status_code, 200)
        self.assertEqual(self.client.post(f"/api/jobs/{identifier}/retry").status_code, 409)
        app.update_job(identifier, status="running", cancel_requested=1)
        with self.assertRaises(Cancelled):
            app.update_job(identifier, status="done")

    def test_polling_neither_loads_snapshots_nor_rehashes_them(self):
        identifier = self.create({"profile": "low"})
        with app.db_conn() as con:
            stored = con.execute("SELECT fingerprint FROM job_configs WHERE job_id=?", (identifier,)).fetchone()[0]
        with patch.object(jobs, "load_config", side_effect=AssertionError("snapshot loaded")), \
             patch.object(config, "fingerprint", side_effect=AssertionError("snapshot rehashed")):
            listed = self.client.get("/api/jobs").json["jobs"][0]
            self.assertEqual(self.client.get(f"/api/jobs/{identifier}/live").status_code, 200)
            self.assertNotIn("configuration", app.get_job(identifier))
        self.assertEqual(listed["configuration"]["fingerprint"], stored)
        # The single-job route still verifies the snapshot before exposing it.
        self.assertEqual(self.client.get(f"/api/jobs/{identifier}").json["configuration"], listed["configuration"])

    def test_pipeline_previews_do_not_reread_the_job(self):
        identifier = self.create()
        with patch.object(app, "get_job", wraps=app.get_job) as reads:
            self.run_job(identifier, 3)
        self.assertEqual(reads.call_count, 1)
        preview = json.loads((app.job_dir(identifier) / "live_preview.json").read_text())
        self.assertEqual(preview["phase"], "Writing final report")

    def test_live_and_download_do_not_generate_and_reasoning_is_opt_in(self):
        identifier = self.create()
        directory = app.job_dir(identifier)
        app.atomic_json(directory / "live_preview.json", {"content": "Answer", "reasoning": "PRIVATE_REASONING",
                                                         "reasoning_chars": 17, "active": True})
        with patch.object(app.ModelClient, "complete", side_effect=AssertionError("No generation")):
            self.assertNotIn("PRIVATE_REASONING", self.client.get(f"/api/jobs/{identifier}/live").get_data(as_text=True))
            self.assertEqual(self.client.get(f"/api/jobs/{identifier}/live?include_reasoning=true").json["reasoning"], "PRIVATE_REASONING")
            self.assertEqual(self.client.get(f"/api/jobs/{identifier}/report").status_code, 409)
            app.update_job(identifier, status="done")
            (directory / "rapport_final.md").write_text("# Report")
            with self.client.get(f"/api/jobs/{identifier}/report") as response:
                self.assertEqual(response.data, b"# Report")

    def test_diagnostics_are_read_only_and_missing_properties_are_supported(self):
        with patch.object(app.BackendClient, "inspect", return_value={"status": "available", "context_tokens": None}), \
             patch.object(app.BackendClient, "complete", side_effect=AssertionError("No generation")):
            payload = self.client.get("/api/diagnostics").json
        self.assertIsNone(payload["backend"]["context_tokens"])
        self.assertEqual(payload["engine_settings"], "read_only")
        self.assertNotIn("LLAMA", json.dumps(payload))

    def test_browser_translation_catalog_and_static_assets_are_packaged(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="profile"', response.get_data(as_text=True))
        self.assertNotIn("KEEP_PRIVATE", response.get_data(as_text=True))
        catalog = json.loads((Path(app.__file__).parent / "static/translations.json").read_text())
        self.assertEqual(set(catalog["fr"]), set(catalog["en"]))
        for path in ("app.js", "app.css", "fr.svg", "en.svg"):
            with self.client.get("/static/" + path) as asset:
                self.assertEqual(asset.status_code, 200)

    def test_cross_origin_posts_and_external_report_symlinks_are_rejected(self):
        self.assertEqual(self.client.post("/api/jobs", headers={"Origin": "https://foreign.invalid"}).status_code, 403)
        identifier = self.create()
        app.update_job(identifier, status="done")
        outside = self.root / "private.txt"
        outside.write_text("private")
        (app.job_dir(identifier) / "rapport_final.md").symlink_to(outside)
        self.assertEqual(self.client.get(f"/api/jobs/{identifier}/report").status_code, 404)


class MergePlanTests(unittest.TestCase):
    """Prove that adaptive grouping preserves order and cannot create a singleton loop."""

    def test_groups_preserve_all_inputs_for_sizes_one_to_twelve(self):
        for count in range(2, 13):
            notes = list(range(count))
            groups = pipeline.plan_groups(notes, lambda group: len(group) <= 3)
            self.assertEqual([note for group in groups for note in group], notes)
            self.assertLess(len(groups), len(notes))
            self.assertTrue(all(len(group) <= 3 for group in groups))

    def test_impossible_pairs_fail_instead_of_looping(self):
        for notes in ([1], [1, 2], [1, 2, 3, 4, 5]):
            with self.assertRaises(ProcessingError):
                pipeline.plan_groups(notes, lambda group: len(group) <= 1)


if __name__ == "__main__":
    unittest.main()
