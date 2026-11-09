"""Resume real persisted attempts with deterministic SSE and exact call counts."""

import copy
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import WordCountModelMixin, sse_event

from document_processor import artifacts, config, extraction, pipeline, reliability
from document_processor.application import create_app
from document_processor.errors import ProcessingError, WorkerStopping
from document_processor.files import atomic_json, file_hash
from document_processor.jobs import JobStore
from document_processor.model_client import ModelClient
from document_processor.observability import log
from document_processor.report_text import REPORT_FILE
from document_processor.settings import Settings

PARTIAL = "INCOMPLETE RESPONSE MUST NEVER BE REUSED"


class ResumeStreamModel(WordCountModelMixin, ModelClient):
    """Keep actual prompt fitting, SSE validation, cancellation and metrics."""

    def inspect(self):
        self.capabilities = {
            "context_tokens": 16384,
            "template_hash": config.fingerprint(self.template),
            "sampling": self.sampling,
            "version": "deterministic-v1",
        }
        return self.capabilities

    def model(self):
        return self.identity

    def fits(self, instruction, content, max_tokens):
        result = super().fits(instruction, content, max_tokens)
        count = content.count("## Notes ")
        self.fit_calls.append((self.current_stage, max_tokens, count))
        if (
            self.current_stage == "merge"
            and self.regroup
            and max_tokens >= 512
            and count > 2
        ):
            return False, *result[1:]
        if (
            self.current_stage == "final"
            and max_tokens >= 512
            and (
                self.final_impossible
                or self.final_compact
                and "Source evidence (untrusted data;" in content
            )
        ):
            return False, *result[1:]
        return result

    def stream_lines(self, payload):
        stage = self.current_stage
        self.stage_calls[stage] += 1
        instruction, content = payload["messages"][-1]["content"].split("\n\n", 1)
        call = {
            "stage": stage,
            "budget": payload["max_tokens"],
            "instruction": instruction,
            "input": content,
        }
        self.calls.append(call)
        self.all_calls.append(call)
        failure = self.failure
        if failure and (stage, self.stage_calls[stage]) == failure[:2]:
            yield "data: " + json.dumps(sse_event({"content": PARTIAL}))
            if failure[2] == "cancelled":
                self.cancel()
                self.check_cancelled()
            if failure[2] == "interrupted":
                raise WorkerStopping()
            if failure[2] == "incomplete_stream":
                return
            yield "data: " + json.dumps(sse_event({}, "length"))
        else:
            if stage == "section":
                items = [int(re.search(r"Excerpt (\d+) of", instruction)[1])]
            else:
                items = sorted(
                    {int(value) for value in re.findall(r"Item (\d+):", content)}
                )
            text = "\n".join(
                f"Item {index}: Measured value 25 °C ± 0.5 °C [Page {index}]. Limitation: one sample."
                for index in items
            )
            if stage == "final":
                text = "# Measurements\n" + text
            yield "data: " + json.dumps(sse_event({"content": text}, "stop"))
        yield "data: [DONE]"


class PipelineResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pipeline-resume-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prompt = self.root / "prompt.txt"
        prompt.write_text("Keep measured values and their physical page references.")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = replace(
                Settings.from_env(),
                data_dir=self.root / "data",
                system_prompt_file=prompt,
                analysis_tokens=256,
                merge_tokens=256,
                final_tokens=256,
                chunk_tokens=512,
                max_job_attempts=8,
            )
        self.models, self.calls = [], []
        self.failure = None
        self.identity, self.template = "resume-model", "saved-template"
        self.sampling = {"temperature": 0.2}
        self.regroup = self.final_compact = self.final_impossible = False
        self.store = JobStore(self.settings.data_dir)
        self.addCleanup(lambda: self.store.close())
        self.store.initialize()
        self.bind_application()
        self.count = 8
        self.pages = [
            {
                "page": index,
                "text": f"Item {index}: Measured value 25 °C ± 0.5 °C. Limitation: one sample. Calibration not mentioned.",
            }
            for index in range(1, self.count + 1)
        ]
        self.chunks = [
            {"text": page["text"], "pages": [page["page"]]} for page in self.pages
        ]
        patcher = patch.object(
            extraction, "extract_pdf", return_value=(self.pages, self.count, [], [])
        )
        self.extract = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(extraction, "make_chunks", return_value=self.chunks)
        self.split = patcher.start()
        self.addCleanup(patcher.stop)

    def bind_application(self):
        self.application = create_app(self.settings, self.store, self.factory)
        self.services = self.application.extensions["document_processor"]
        self.client = self.application.test_client()

    def factory(self, snapshot):
        model = ResumeStreamModel(
            "http://unused", "", snapshot, snapshot["system_prompt"]
        )
        model.identity, model.template = self.identity, self.template
        model.sampling = copy.deepcopy(self.sampling)
        model.failure = self.failure
        model.regroup, model.final_compact, model.final_impossible = (
            self.regroup,
            self.final_compact,
            self.final_impossible,
        )
        model.calls, model.fit_calls, model.stage_calls = [], [], Counter()
        model.all_calls = self.calls
        model.cancel = lambda: self.client.post(f"/api/jobs/{self.job_id}/cancel")
        self.models.append(model)
        return model

    def create(self):
        response = self.client.post(
            "/api/jobs",
            data={
                "file": (io.BytesIO(b"%PDF-deterministic"), "study.pdf"),
                "question": "Report measurements.",
            },
        )
        self.assertEqual(response.status_code, 201, response.json)
        self.job_id = response.json["id"]
        self.original = self.store.load_config(self.job_id)
        self.original_root = (
            self.store.job_dir(self.job_id)
            / "artifacts"
            / config.fingerprint(self.original)
        )
        return self.job_id

    def execute(self):
        start = len(self.calls)
        self.assertEqual(self.store.claim_next_job(), self.job_id)
        with patch.object(log, "error"):
            self.services.worker.run_claimed(self.job_id)
        return self.calls[start:]

    def resume(self, **budgets):
        with patch.object(self.services.worker, "wake"):
            response = self.client.post(f"/api/jobs/{self.job_id}/resume", json=budgets)
        self.assertEqual(response.status_code, 200, response.json)
        self.failure = None

    def retry(self):
        with patch.object(self.services.worker, "wake"):
            response = self.client.post(f"/api/jobs/{self.job_id}/retry")
        self.assertEqual(response.status_code, 200, response.json)
        self.failure = None

    def restart(self):
        self.store.close()
        self.store = JobStore(self.settings.data_dir)
        self.store.initialize()
        self.bind_application()

    def steps(self):
        metrics = self.store.metrics(self.job_id, limit=500)
        current = metrics["runs"][0]
        return current, {
            step["step_key"]: step
            for step in metrics["steps"]
            if step["run_id"] == current["id"]
        }

    def preserved(self):
        return {
            path.name: path.read_bytes()
            for path in self.original_root.iterdir()
            if path.is_file()
        }

    def assert_calls(self, calls, **expected):
        self.assertEqual(Counter(call["stage"] for call in calls), Counter(expected))

    def assert_done(self):
        self.assertEqual(self.store.get_job(self.job_id)["status"], "done")
        report = (self.store.job_dir(self.job_id) / REPORT_FILE).read_text()
        self.assertNotIn(PARTIAL, report)
        for index in range(1, self.count + 1):
            self.assertIn(f"[Page {index}]", report)
        current, steps = self.steps()
        self.assertEqual(current["status"], "done")
        self.assertEqual(
            current["provenance"]["generation_calls"],
            sum(
                step["status"] == "done" and step["stage"] in pipeline.GENERATION_STAGES
                for step in steps.values()
            ),
        )

    def assert_reused(self, key, budget=None, revision=0):
        _, steps = self.steps()
        step = steps[key]
        self.assertEqual(step["status"], "reused")
        self.assertTrue(step["metrics"]["artifact_reused"])
        self.assertEqual(step["metrics"]["artifact_source_revision"], revision)
        source = self.store.load_config(self.job_id, revision=revision)
        self.assertEqual(
            step["metrics"]["artifact_source_config_hash"], config.fingerprint(source)
        )
        if budget is not None:
            self.assertEqual(step["metrics"]["output_budget"], budget)
            self.assertNotIn(
                "output_tokens", step["metrics"]
            )  # Never charge old usage to this run.

    def test_fresh_baseline_is_eleven_calls(self):
        for budget in (256, 512):
            with self.subTest(budget=budget):
                self.settings = replace(
                    self.settings,
                    analysis_tokens=budget,
                    merge_tokens=budget,
                    final_tokens=budget,
                )
                self.bind_application()
                self.create()
                self.assert_calls(self.execute(), section=8, merge=2, final=1)
                self.assert_done()

    def test_merge_output_limit_reuses_sections_and_only_compatible_completed_merges(
        self,
    ):
        for failing_merge, remaining in ((1, 2), (2, 1)):
            with self.subTest(failing_merge=failing_merge):
                self.create()
                self.failure = ("merge", failing_merge, "output_limit")
                self.assert_calls(self.execute(), section=8, merge=failing_merge)
                self.assertEqual(
                    self.store.get_job(self.job_id)["error_code"], "output_limit"
                )
                before = self.preserved()
                self.resume(merge_tokens=512)
                self.assert_calls(self.execute(), merge=remaining, final=1)
                self.assert_done()
                for index in range(self.count):
                    self.assert_reused(f"analysis_{index:04d}.md", 256)
                if failing_merge == 2:
                    self.assert_reused("merge_00_0000.md", 256)
                    self.assertEqual(
                        self.steps()[1]["merge_00_0000.md"]["metrics"][
                            "requested_output_budget"
                        ],
                        512,
                    )
                self.assert_reused("extracted.json")
                self.assert_reused("chunks.json")
                self.assertEqual(self.preserved(), before)
                self.assertTrue(
                    all(
                        call["budget"] == 512
                        for call in self.models[-1].calls
                        if call["stage"] == "merge"
                    )
                )

    def test_section_failure_reuses_three_sections_and_applies_new_budget_to_remaining(
        self,
    ):
        self.create()
        self.failure = ("section", 4, "output_limit")
        self.assert_calls(self.execute(), section=4)
        self.assertFalse((self.original_root / "analysis_0003.md").exists())
        self.resume(analysis_tokens=512)
        self.assert_calls(self.execute(), section=5, merge=2, final=1)
        self.assert_done()
        for index in range(3):
            self.assert_reused(f"analysis_{index:04d}.md", 256)
        self.assertEqual(
            [
                call["budget"]
                for call in self.models[-1].calls
                if call["stage"] == "section"
            ],
            [512] * 5,
        )

    def test_final_output_limit_reuses_ten_generations_and_checks_outputs_again(self):
        self.create()
        self.failure = ("final", 1, "output_limit")
        self.assert_calls(self.execute(), section=8, merge=2, final=1)
        self.assertFalse((self.original_root / REPORT_FILE).exists())
        self.resume(final_tokens=512)
        self.extract.reset_mock()
        self.split.reset_mock()
        with patch.object(reliability, "audit", wraps=reliability.audit) as audit:
            self.assert_calls(self.execute(), final=1)
        self.assertEqual(audit.call_count, 11)
        self.assert_done()
        self.extract.assert_not_called()
        self.split.assert_not_called()
        self.assertEqual(self.models[-1].calls[0]["budget"], 512)
        self.assert_reused("merge_00_0000.md", 256)
        self.assert_reused("merge_00_0001.md", 256)

    def test_changed_merge_grouping_invalidates_same_named_merge(self):
        self.create()
        self.failure = ("merge", 2, "output_limit")
        self.execute()
        self.regroup = True
        self.resume(merge_tokens=512)
        self.assert_calls(self.execute(), merge=4, final=1)
        self.assert_done()
        self.assertEqual(self.steps()[1]["merge_00_0000.md"]["status"], "done")
        self.assertEqual(
            [
                call["input"].count("## Notes ")
                for call in self.models[-1].calls
                if call["stage"] == "merge"
            ],
            [2] * 4,
        )
        self.assertIn(("merge", 512, 3), self.models[-1].fit_calls)
        self.settings = replace(self.settings, merge_tokens=512)
        self.bind_application()
        self.create()
        self.assert_calls(self.execute(), section=8, merge=4, final=1)
        self.assert_done()

    def test_cancel_during_verified_reuse_stops_before_any_new_generation(self):
        self.create()
        self.failure = ("final", 1, "output_limit")
        self.execute()
        self.resume(final_tokens=512)
        read = artifacts.Artifacts._read_previous

        def cancel_after_read(root, name, *args):
            result = read(root, name, *args)
            if name == "analysis_0001.md" and result is not None:
                self.client.post(f"/api/jobs/{self.job_id}/cancel")
            return result

        with patch.object(
            artifacts.Artifacts, "_read_previous", side_effect=cancel_after_read
        ):
            self.assertEqual(self.execute(), [])
        self.assertEqual(self.store.get_job(self.job_id)["status"], "cancelled")
        self.assertEqual(self.steps()[1]["analysis_0001.md"]["status"], "cancelled")
        self.retry()
        self.assert_calls(self.execute(), final=1)
        self.assert_done()

    def test_successive_budget_revisions_keep_each_original_generation_budget(self):
        self.create()
        self.failure = ("section", 4, "output_limit")
        self.assert_calls(self.execute(), section=4)
        self.resume(analysis_tokens=512)
        self.failure = ("section", 2, "output_limit")
        self.assert_calls(self.execute(), section=2)
        self.resume(analysis_tokens=1024)
        self.assert_calls(self.execute(), section=4, merge=2, final=1)
        self.assert_done()
        for index in range(3):
            self.assert_reused(f"analysis_{index:04d}.md", 256)
        self.assert_reused("analysis_0003.md", 512, revision=1)
        _, steps = self.steps()
        self.assertEqual(
            steps["analysis_0003.md"]["metrics"]["requested_output_budget"], 1024
        )
        self.assertEqual(steps["analysis_0004.md"]["metrics"]["output_budget"], 1024)

    def fail_saving(self):
        self.create()
        with patch.object(
            pipeline,
            "report_filename",
            side_effect=ProcessingError("save_error", "Cannot save name."),
        ):
            self.assert_calls(self.execute(), section=8, merge=2, final=1)
        self.assertEqual(self.store.get_job(self.job_id)["status"], "failed")

    def test_completed_final_reused_after_save_failure_and_previous_report_archived(
        self,
    ):
        self.fail_saving()
        directory = self.store.job_dir(self.job_id)
        previous_report = (directory / REPORT_FILE).read_bytes()
        previous_hash = file_hash(directory / REPORT_FILE)
        before = self.preserved()
        self.resume(final_tokens=512)
        self.assertEqual(self.execute(), [])
        self.assert_done()
        self.assert_reused(REPORT_FILE, 256)
        self.assertEqual(
            (directory / "previous_reports" / f"{previous_hash}.md").read_bytes(),
            previous_report,
        )
        self.assertEqual(self.preserved(), before)

    def test_retry_preserves_report_archive_snapshot_and_external_temporary_targets(
        self,
    ):
        self.fail_saving()
        directory = self.store.job_dir(self.job_id)
        previous = (directory / REPORT_FILE).read_bytes()
        archive = directory / "previous_reports"
        archive.mkdir()
        archived_report = archive / f"{file_hash(directory / REPORT_FILE)}.md"
        external = self.root / "external.txt"
        external.write_text("EXTERNAL CONTENT", encoding="utf-8")
        destinations = (
            directory / REPORT_FILE,
            directory / "live_preview.json",
            self.original_root / "manifest.json",
            self.original_root / "quality_checks.json",
            archived_report,
        )
        links = [path.with_suffix(path.suffix + ".tmp") for path in destinations]
        for link in links:
            link.symlink_to(external)
        self.retry()
        self.assertEqual(self.execute(), [])
        self.assert_done()
        self.assertEqual(external.read_text(), "EXTERNAL CONTENT")
        self.assertTrue(all(link.is_symlink() for link in links))
        self.assertEqual(archived_report.read_bytes(), previous)
        self.assertEqual(self.store.load_config(self.job_id), self.original)

    def test_changed_final_input_preparation_invalidates_completed_final(self):
        self.fail_saving()
        self.final_compact = True
        self.resume(final_tokens=512)
        self.assert_calls(self.execute(), final=1)
        self.assert_done()
        self.assertIn(
            "Source evidence omitted from this prompt",
            self.models[-1].calls[0]["input"],
        )
        self.assertEqual(self.steps()[1][REPORT_FILE]["status"], "done")

    def test_replaced_final_after_digest_cannot_publish_unverified_content(self):
        self.fail_saving()
        path = self.original_root / REPORT_FILE
        verified = path.read_text(encoding="utf-8")
        replacement = self.root / "replacement.md"
        replacement.write_text("UNVERIFIED FINAL REPORT", encoding="utf-8")
        sha256 = hashlib.sha256
        swapped = False

        def hash_bytes(content):
            nonlocal swapped
            digest = sha256(content)
            if content == verified.encode("utf-8"):
                os.replace(replacement, path)
                swapped = True
            return digest

        def hash_path(candidate):
            nonlocal swapped
            digest = file_hash(candidate)
            if candidate == path:
                os.replace(replacement, path)
                swapped = True
            return digest

        self.retry()
        with (
            patch.object(artifacts, "file_hash", side_effect=hash_path),
            patch.object(artifacts, "hashlib", SimpleNamespace(sha256=hash_bytes)),
        ):
            self.assertEqual(self.execute(), [])
        self.assertTrue(swapped, "The substitution must occur during verification")
        self.assert_done()
        self.assert_reused(REPORT_FILE, 256)
        report = (self.store.job_dir(self.job_id) / REPORT_FILE).read_text()
        self.assertIn(verified, report)
        self.assertNotIn("UNVERIFIED FINAL REPORT", report)
        self.assertEqual(path.read_text(), "UNVERIFIED FINAL REPORT")

    def test_revised_final_context_is_rechecked_before_generation(self):
        self.create()
        self.failure = ("final", 1, "output_limit")
        self.execute()
        self.final_impossible = True
        self.resume(final_tokens=512)
        self.assert_calls(self.execute(), merge=1)
        self.assertEqual(
            self.store.get_job(self.job_id)["error_code"], "merge_impossible"
        )

    def test_corrupt_or_incomplete_artifact_and_dependency_recompute_only_affected_step(
        self,
    ):
        for name, mode, expected in (
            ("analysis_0000.md", "content", {"section": 1, "final": 1}),
            ("analysis_0000.md", "dependency", {"section": 1, "final": 1}),
            ("analysis_0000.md", "incomplete", {"section": 1, "final": 1}),
            ("merge_00_0000.md", "content", {"merge": 1, "final": 1}),
            ("merge_00_0000.md", "missing", {"merge": 1, "final": 1}),
            ("extracted.json", "content", {"final": 1}),
            ("chunks.json", "content", {"final": 1}),
            ("source_evidence.json", "content", {"final": 1}),
        ):
            with self.subTest(name=name, mode=mode):
                self.create()
                self.failure = ("final", 1, "output_limit")
                self.execute()
                path = self.original_root / name
                if mode == "content":
                    path.write_text("CORRUPTED")
                elif mode == "missing":
                    path.unlink()
                else:
                    manifest_path = self.original_root / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest["artifacts"][name][
                        "dependencies" if mode == "dependency" else "complete"
                    ] = "changed" if mode == "dependency" else False
                    atomic_json(manifest_path, manifest)
                before = self.preserved()
                self.resume(final_tokens=512)
                self.assert_calls(self.execute(), **expected)
                self.assert_done()
                self.assertEqual(self.preserved(), before)

    def test_changed_identity_rejects_cross_revision_artifacts_and_retry_still_fails_safe(
        self,
    ):
        for field in ("identity", "template", "sampling"):
            with self.subTest(field=field):
                self.identity, self.template, self.sampling = (
                    "resume-model",
                    "saved-template",
                    {"temperature": 0.2},
                )
                self.create()
                self.failure = ("final", 1, "output_limit")
                self.execute()
                before = self.preserved()
                setattr(
                    self,
                    field,
                    {"temperature": 0.7} if field == "sampling" else "changed",
                )
                self.retry()
                self.assertEqual(self.execute(), [])
                self.assertEqual(
                    self.store.get_job(self.job_id)["error_code"], "identity_changed"
                )
                self.resume(final_tokens=512)
                self.assert_calls(self.execute(), section=8, merge=2, final=1)
                self.assert_done()
                self.assertEqual(self.preserved(), before)

    def test_changed_source_fails_before_any_new_generation(self):
        self.create()
        self.failure = ("final", 1, "output_limit")
        self.execute()
        self.resume(final_tokens=512)
        (self.store.job_dir(self.job_id) / "source.pdf").write_bytes(b"Changed PDF")
        self.assertEqual(self.execute(), [])
        self.assertEqual(
            self.store.get_job(self.job_id)["error_code"], "source_changed"
        )

    def test_cancel_resume_restart_and_retry_preserve_origins_and_never_continue_partial(
        self,
    ):
        self.create()
        self.failure = ("section", 4, "cancelled")
        self.assert_calls(self.execute(), section=4)
        self.assertEqual(self.store.get_job(self.job_id)["status"], "cancelled")
        self.assertFalse((self.original_root / "analysis_0003.md").exists())
        self.resume(analysis_tokens=512)
        revised = self.store.load_config(self.job_id)
        self.restart()
        self.failure = ("final", 1, "incomplete_stream")
        self.assert_calls(self.execute(), section=5, merge=2, final=1)
        self.assertEqual(
            self.store.get_job(self.job_id)["error_code"], "incomplete_stream"
        )
        revised_root = (
            self.store.job_dir(self.job_id) / "artifacts" / config.fingerprint(revised)
        )
        self.assertFalse((revised_root / REPORT_FILE).exists())
        self.retry()
        self.assertEqual(self.store.load_config(self.job_id), revised)
        self.assert_calls(self.execute(), final=1)
        self.assert_done()
        for index in range(self.count):
            self.assert_reused(
                f"analysis_{index:04d}.md",
                256 if index < 3 else 512,
                revision=0 if index < 3 else 1,
            )
        metrics = self.store.metrics(self.job_id)
        self.assertEqual(
            [run["status"] for run in metrics["runs"]], ["done", "failed", "cancelled"]
        )
        for run, expected in zip(
            metrics["runs"], (revised, revised, self.original), strict=True
        ):
            self.assertEqual(
                self.store.load_attempt_config(self.job_id, run["id"]), expected
            )
        self.assertTrue(all(PARTIAL not in call["input"] for call in self.calls))

    def test_restart_recovers_interrupted_selected_revision_and_completed_work(self):
        self.create()
        self.failure = ("section", 4, "output_limit")
        self.execute()
        self.resume(analysis_tokens=512)
        self.failure = (
            "section",
            2,
            "interrupted",
        )  # Section 4 finishes; section 5 is cut short.
        self.assert_calls(self.execute(), section=2)
        self.assertEqual(self.store.get_job(self.job_id)["status"], "running")
        revised = self.store.load_config(self.job_id)
        self.restart()
        self.failure = None
        self.assertEqual(self.store.get_job(self.job_id)["status"], "queued")
        self.assert_calls(self.execute(), section=4, merge=2, final=1)
        self.assert_done()
        self.assert_reused("analysis_0003.md", 512, revision=1)
        self.assertEqual(self.store.load_config(self.job_id), revised)

    def test_retry_limit_blocks_resume_and_retry_without_extra_calls(self):
        self.settings = replace(self.settings, max_job_attempts=2)
        self.bind_application()
        self.create()
        self.failure = ("final", 1, "output_limit")
        self.execute()
        self.resume(final_tokens=512)
        self.failure = ("final", 1, "output_limit")
        self.assert_calls(self.execute(), final=1)
        before = len(self.calls)
        for action, kwargs in (
            ("retry", {}),
            ("resume", {"json": {"final_tokens": 1024}}),
        ):
            response = self.client.post(f"/api/jobs/{self.job_id}/{action}", **kwargs)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(response.json["error_code"], "retry_limit")
        self.assertEqual(self.store.attempt_count(self.job_id), 2)
        self.assertEqual(len(self.calls), before)

    def test_reuse_verifies_history_pdf_and_manifest_once_per_attempt(self):
        self.create()
        self.failure = ("final", 1, "output_limit")
        self.execute()
        self.resume(final_tokens=512)
        reads = Counter()
        regular_file = artifacts._regular_file

        @contextmanager
        def observe(parent, name):
            with regular_file(parent, name) as stream:
                read = Mock(wraps=stream.read)
                yield SimpleNamespace(read=read, fileno=stream.fileno)
                reads[name] += read.call_count

        with (
            patch.object(artifacts, "_regular_file", side_effect=observe),
            patch.object(
                self.store,
                "load_artifact_history",
                wraps=self.store.load_artifact_history,
            ) as history,
        ):
            self.assert_calls(self.execute(), final=1)
        self.assert_done()
        self.assertEqual(history.call_count, 1)
        self.assertEqual(reads["source.pdf"], 1)
        self.assertEqual(reads["manifest.json"], 1)
        self.assertEqual(reads["analysis_0000.md"], 1)
        self.assertEqual(reads["source_evidence.json"], 1)
        # Once this revision has local results, retrying also avoids historical lookup for them.
        self.store.update_job(self.job_id, status="failed")
        self.retry()
        with patch.object(
            artifacts.Artifacts,
            "read_compatible",
            autospec=True,
            side_effect=artifacts.Artifacts.read_compatible,
        ) as lookup:
            # Earlier artifacts still live only in revision 0; the local final skips lookup.
            self.assertEqual(self.execute(), [])
        self.assertNotIn(REPORT_FILE, [call.args[1] for call in lookup.call_args_list])


if __name__ == "__main__":
    unittest.main()
