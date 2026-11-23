"""Disabled previews retain real parsing, metrics, step recording and artifacts."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from document_processor import model_client, pipeline, reliability
from document_processor.errors import Cancelled, ModelError, WorkerStopping
from document_processor.files import file_hash
from document_processor.jobs import JobStore
from document_processor.model_client import ModelClient, StreamParser, ThinkingSplitter


def exercise(live_output, scenario="success", count=4, interval=1.0):
    """Drive a real pipeline step and SSE parser with an event-driven clock."""
    with tempfile.TemporaryDirectory() as name:
        store = JobStore(Path(name))
        store.initialize()
        job_id = "a" * 32
        directory = store.job_dir(job_id)
        directory.mkdir(parents=True)
        (directory / "source.pdf").write_bytes(b"%PDF-offline")
        config = {
            "pipeline_version": 2,
            "source_hash": file_hash(directory / "source.pdf"),
            "max_attempts": 3,
            "max_calls": 10,
            "analysis_tokens": 4096,
            "merge_tokens": 4096,
            "final_tokens": 4096,
            "context_tokens": 8192,
            "step_timeout_seconds": 10,
            "request_timeout": 10,
        }
        store.create_job(job_id, "fixture.pdf", "Analyze", 1, config)
        store.claim_next_job()
        model = ModelClient(
            "http://unused.invalid", "", config, "system", preview_limit=8
        )
        model.model_name = "offline"
        runtime = pipeline.Runtime(
            store, Mock(), lambda config: model, store.read_cancellation, live_output, 8
        )
        run = pipeline.JobRun(job_id, runtime)
        run.evidence = reliability.build_evidence([], 1)
        clock = [1000.0]
        work = {
            "snapshots": 0,
            "content_assemblies": 0,
            "assembled_chars": 0,
            "preview_writes": 0,
        }
        payloads = []
        errors = {
            "backend": ModelError("stream_error", "Simulated disconnect."),
            "deadline": ModelError("deadline", "Simulated deadline."),
            "cancelled": Cancelled(),
            "interrupted": WorkerStopping(),
        }

        def stream(payload):
            for index in range(count):
                if index == 2 and scenario in ("cancelled", "interrupted"):
                    model.cancel_check = Mock(side_effect=errors[scenario])
                model.check_cancelled()
                clock[0] += interval
                event = {
                    "choices": [
                        {"delta": {"content": "abcdefgh", "reasoning_content": "réfl"}}
                    ]
                }
                if index == count - 1:
                    if scenario in ("success", "length"):
                        event["choices"][0]["finish_reason"] = (
                            "stop" if scenario == "success" else "length"
                        )
                    event["usage"] = {
                        "prompt_tokens": 10,
                        "completion_tokens": count,
                        "completion_tokens_details": {"reasoning_tokens": count},
                    }
                yield "data: " + json.dumps(event)
            if scenario in ("backend", "deadline"):
                raise errors[scenario]
            if scenario == "malformed":
                yield "data: {"
            elif scenario != "incomplete":
                yield "data: [DONE]"

        content = ThinkingSplitter.content.fget
        state = StreamParser.state.fget
        persist = pipeline.atomic_json
        publish = store.previews.put

        def observed_content(splitter):
            value = content(splitter)
            work["content_assemblies"] += 1
            work["assembled_chars"] += len(value)
            return value

        def observed_state(parser):
            work["snapshots"] += 1
            return state(parser)

        def observed_write(path, payload):
            if Path(path) == run.preview_path:
                work["preview_writes"] += 1
            persist(path, payload)

        def observed_publish(*args):
            payloads.append(copy.deepcopy(args[1]))
            return publish(*args)

        error = None
        answer = None
        try:
            with (
                patch.object(
                    model_client.time, "monotonic", side_effect=lambda: clock[0]
                ),
                patch.object(model_client.time, "time", return_value=1700000000.0),
                patch.object(
                    model, "json_request", return_value={"tokens": list(range(10))}
                ),
                patch.object(model, "stream_lines", side_effect=stream),
                patch.object(model, "complete", wraps=model.complete) as complete,
                patch.object(ThinkingSplitter, "content", property(observed_content)),
                patch.object(StreamParser, "state", property(observed_state)),
                patch.object(pipeline, "atomic_json", side_effect=observed_write),
                patch.object(store.previews, "put", side_effect=observed_publish),
            ):
                try:
                    answer = run._step(
                        "analysis_0000.md",
                        "section",
                        {"source": "fixture"},
                        lambda: run._generate(
                            "section", "instruction", "source", 4096, [1]
                        ),
                    )
                except (ModelError, Cancelled, WorkerStopping) as exc:
                    error = exc
                callback = complete.call_args.kwargs["on_update"]
            artifact = run.artifacts.root / "analysis_0000.md"
            return {
                "answer": answer,
                "error_type": type(error).__name__ if error else None,
                "error_code": getattr(error, "code", None),
                "metrics": copy.deepcopy(model.current_metrics),
                "step": store.metrics(job_id)["steps"][0],
                "artifact": artifact.read_bytes() if artifact.exists() else None,
                "manifest": copy.deepcopy(run.artifacts.manifest),
                "callback_present": callback is not None,
                "preview_exists": run.preview_path.exists(),
                "payloads": payloads,
                "work": work,
            }
        finally:
            run.recorder.finish("failed" if error else "done")
            model.session.close()
            store.close()


class DisabledPreviewTests(unittest.TestCase):
    def test_large_answer_avoids_all_preview_assembly_when_disabled(self):
        enabled = exercise(True, count=1000)
        disabled = exercise(False, count=1000)
        self.assertEqual(enabled["answer"], "abcdefgh" * 1000)
        self.assertEqual(disabled["answer"], enabled["answer"])
        self.assertFalse(disabled["callback_present"])
        self.assertEqual(
            disabled["work"],
            {
                "snapshots": 0,
                "content_assemblies": 1,
                "assembled_chars": 8000,
                "preview_writes": 0,
            },
        )
        self.assertEqual(enabled["work"]["snapshots"], 1001)
        self.assertEqual(enabled["work"]["assembled_chars"], 4020000)
        self.assertTrue(enabled["callback_present"])
        self.assertFalse(disabled["preview_exists"])
        self.assertEqual(disabled["payloads"], [])
        self.assertEqual(disabled["metrics"], enabled["metrics"])
        self.assertEqual(disabled["step"]["status"], "done")
        self.assertEqual(enabled["step"]["status"], "done")
        self.assertEqual(disabled["step"]["metrics"], enabled["step"]["metrics"])
        for field in ("artifact", "manifest"):
            self.assertEqual(disabled[field], enabled[field])
        self.assertEqual(disabled["metrics"]["output_tokens"], 1000)
        self.assertEqual(disabled["metrics"]["content_chars"], 8000)
        self.assertEqual(disabled["metrics"]["reasoning_chars"], 4000)

    def test_enabled_previews_remain_throttled_bounded_and_terminal(self):
        result = exercise(True, count=100, interval=0.01)
        self.assertGreater(result["work"]["snapshots"], 1)
        self.assertLessEqual(result["work"]["snapshots"], 4)
        self.assertTrue(result["preview_exists"])
        self.assertTrue(result["payloads"][0]["active"])
        self.assertFalse(result["payloads"][-1]["active"])
        self.assertEqual(result["payloads"][-1]["content"], "abcdefgh")
        self.assertEqual(result["payloads"][-1]["content_chars"], 800)
        self.assertEqual(result["payloads"][-1]["reasoning_chars"], 400)
        for payload in result["payloads"]:
            self.assertLessEqual(len(payload["content"]), 8)
            self.assertLessEqual(len(payload["reasoning"]), 8)

    def test_error_cancel_shutdown_and_deadline_keep_metrics_steps_and_no_artifacts(
        self,
    ):
        cases = {
            "backend": ("stream_error", "failed", "error"),
            "deadline": ("deadline", "failed", "error"),
            "cancelled": ("cancelled", "cancelled", "cancelled"),
            "interrupted": (None, "interrupted", "interrupted"),
            "length": ("output_limit", "failed", "error"),
            "incomplete": ("incomplete_stream", "failed", "error"),
            "malformed": ("invalid_stream", "failed", "error"),
        }
        for scenario, (code, status, stop) in cases.items():
            with self.subTest(scenario=scenario):
                enabled = exercise(True, scenario)
                disabled = exercise(False, scenario)
                self.assertEqual(disabled["error_type"], enabled["error_type"])
                self.assertEqual(disabled["error_code"], code)
                self.assertEqual(disabled["metrics"], enabled["metrics"])
                self.assertEqual(
                    disabled["step"]["metrics"], enabled["step"]["metrics"]
                )
                self.assertEqual(disabled["step"]["status"], status)
                self.assertEqual(disabled["metrics"]["stop_category"], stop)
                self.assertIsNone(disabled["artifact"])
                self.assertIsNone(enabled["artifact"])
                self.assertEqual(disabled["manifest"], enabled["manifest"])
                self.assertEqual(disabled["work"]["snapshots"], 0)
                self.assertEqual(disabled["work"]["preview_writes"], 0)
                self.assertFalse(disabled["preview_exists"])
                self.assertEqual(disabled["payloads"], [])
                self.assertFalse(enabled["payloads"][-1]["active"])


if __name__ == "__main__":
    unittest.main()
