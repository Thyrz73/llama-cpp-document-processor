"""Test the SSE stream and legacy jobs without llama.cpp."""

import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pdf-live-test-"))
os.environ.setdefault(
    "SYSTEM_PROMPT_FILE",
    str(Path(__file__).resolve().parents[1] / "config/system_prompt.example.txt"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from app import ModelClient
from document_processor import pipeline, previews, reliability
from document_processor.errors import Cancelled, ModelError, WorkerStopping
from document_processor.files import atomic_json, file_hash
from document_processor.jobs import JobStore
from document_processor.model_client import ThinkingSplitter


class FakeStream:
    """Simulate a streaming HTTP response."""

    def __init__(self, events):
        """Store events from the fake stream."""
        self.events = events

    def __enter__(self):
        """Return the fake stream when entering a with block."""
        return self

    def __exit__(self, *args):
        """Let exceptions propagate when leaving the with block."""
        return False

    def raise_for_status(self):
        """Simulate a successful HTTP status without raising."""
        return None

    def iter_lines(self, **kwargs):
        """Emit SSE events and their completion marker.

        Accept kwargs used by requests without needing them for this fake
        stream. Return an iterator of bytes.
        """
        for event in self.events:
            yield ("data: " + json.dumps(event)).encode()
        yield b"data: [DONE]"


def event(delta, finish=None):
    """Create an SSE event with delta and an optional finish reason."""
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


class LiveTests(unittest.TestCase):
    """Verify the SSE stream and legacy databases."""

    def test_tags_can_split_at_any_position(self):
        """Recognize a <think> tag split across stream fragments."""
        for split in range(1, len("<think>") + 1):
            parser = ThinkingSplitter()
            text = "Before <think>work</think> After"
            for fragment in [text[: 7 + split], text[7 + split :]]:
                parser.feed(fragment)
            parser.finish()
            self.assertEqual(parser.content, "Before  After")
            self.assertEqual(parser.reasoning, "work")

    def test_separated_reasoning_field(self):
        """Separate reasoning from final text."""
        client = ModelClient()
        events = [
            event({"reasoning_content": "checking"}),
            event({"content": "Answer "}),
            event({"content": "done"}, finish="stop"),
        ]
        preview = []
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="qwen"),
            patch.object(client.session, "post", return_value=FakeStream(events)),
        ):
            result = client.complete(
                "Analyze",
                "PDF",
                max_tokens=50,
                on_update=lambda content, reasoning, done: preview.append(
                    (content, reasoning, done)
                ),
            )
        self.assertEqual(result, "Answer done")
        self.assertEqual(preview[-1], ("Answer done", "checking", True))

    def test_raw_thinking_tags(self):
        """Separate text from reasoning tags."""
        client = ModelClient()
        events = [
            event({"content": "<thi"}),
            event({"content": "nk>working</think>Result"}, finish="stop"),
        ]
        preview = []
        with (
            patch.object(client, "tokenize", return_value=5),
            patch.object(client, "model", return_value="qwen"),
            patch.object(client.session, "post", return_value=FakeStream(events)),
        ):
            result = client.complete(
                "Analyze",
                "PDF",
                100,
                on_update=lambda content, reasoning, done: preview.append(
                    (content, reasoning, done)
                ),
            )
        self.assertEqual(result, "Result")
        self.assertEqual(preview[-1][1], "working")

    def test_truncated_output_is_not_accepted(self):
        """Reject output truncated by the token limit."""
        client = ModelClient()
        events = [event({"content": "Incomplete"}, finish="length")]
        with (
            patch.object(client, "tokenize", return_value=5),
            patch.object(client, "model", return_value="qwen"),
            patch.object(client.session, "post", return_value=FakeStream(events)),
        ):
            with self.assertRaisesRegex(RuntimeError, "max_tokens"):
                client.complete("Analyze", "PDF", 100)

    def test_existing_jobs_table_is_migrated(self):
        """Migrate the table while preserving legacy jobs.

        Create a temporary database without the progress column. Verify that
        legacy jobs are presented at 100 percent through the API while the
        stored value and timestamp remain unchanged.
        """
        import app

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "store", JobStore(Path(tmp))):
                with app.store.connect() as con:
                    con.execute("""CREATE TABLE jobs (
                        id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
                        status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                        pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
                        error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )""")
                    con.execute(
                        """INSERT INTO jobs
                        (id, filename, question, status, stage, created_at, updated_at)
                        VALUES (?, 'example.pdf', 'Analyze', 'done', 'Analysis completed', ?, ?)""",
                        ("a" * 32, "2025-01-01", "2025-01-01"),
                    )
                app.store.initialize()
                with app.store.connect() as con:
                    fields = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
                    stored = dict(con.execute("SELECT * FROM jobs").fetchone())
                self.assertIn("progress_percent", fields)
                self.assertEqual(stored["progress_percent"], 0)
                self.assertEqual(stored["updated_at"], "2025-01-01")
                self.assertEqual(app.store.get_job("a" * 32)["progress_percent"], 100)
                client = app.app.test_client()
                self.assertEqual(
                    client.get("/api/jobs").get_json()["jobs"][0]["progress_percent"],
                    100,
                )
                self.assertEqual(
                    client.get("/api/jobs/" + "a" * 32).get_json()["progress_percent"],
                    100,
                )


class LegacyPreviewTests(unittest.TestCase):
    """Upgrade a pre-attempt database without relabelling or removing old files."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.identifiers = {
            status: character * 32
            for status, character in (
                ("done", "a"),
                ("failed", "b"),
                ("cancelled", "c"),
                ("running", "d"),
            )
        }
        # Original jobs schema: no snapshots, attempts, phases or queued_at.
        with self.store.connect() as con:
            con.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
                error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            for status, identifier in self.identifiers.items():
                con.execute(
                    "INSERT INTO jobs (id,filename,question,status,stage,created_at,updated_at) "
                    "VALUES (?, 'legacy.pdf', 'Analyze', ?, 'Historical stage', '2025-01-01', '2025-01-01')",
                    (identifier, status),
                )
        self.payloads = {}
        for status, identifier in self.identifiers.items():
            directory = self.store.job_dir(identifier)
            directory.mkdir(parents=True)
            (directory / "source.pdf").write_bytes(b"%PDF-legacy-source")
            (directory / "rapport_final.md").write_text(
                "Historical report", encoding="utf-8"
            )
            archive = directory / "previous_reports"
            archive.mkdir()
            (archive / "retained.md").write_text("Archived report", encoding="utf-8")
            artifacts = directory / "artifacts" / "historical"
            artifacts.mkdir(parents=True)
            atomic_json(artifacts / "manifest.json", {"version": 1, "artifacts": {}})
            (artifacts / "analysis_0000.md").write_text(
                "Reusable historical notes", encoding="utf-8"
            )
            self.payloads[identifier] = {
                "phase": "Historical stage",
                "content": f"LEGACY_{status}_TEXT",
                "reasoning": f"LEGACY_{status}_REASONING",
                "reasoning_chars": 30,
                "active": True,
            }
            atomic_json(directory / "live_preview.json", self.payloads[identifier])
        self.files_before = self.files()
        self.store.initialize()
        for name, value in (("store", self.store), ("LIVE_OUTPUT_ENABLED", True)):
            changed = patch.object(app, name, value)
            changed.start()
            self.addCleanup(changed.stop)
        self.client = app.app.test_client()

    def files(self):
        return {
            path.relative_to(self.store.data_dir): path.read_bytes()
            for path in (self.store.data_dir / "jobs").rglob("*")
            if path.is_file()
        }

    def live(self, identifier, reasoning=False):
        response = self.client.get(
            f"/api/jobs/{identifier}/live"
            + ("?include_reasoning=true" if reasoning else "")
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("_attempt_id", payload)
        return payload

    def retry(self, identifier):
        with patch.object(app.worker, "wake"):
            response = self.client.post(f"/api/jobs/{identifier}/retry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(), {"status": "queued", "warnings": ["legacy_recomputed"]}
        )

    def assert_empty(self, identifier):
        job = self.store.get_job(identifier)
        self.assertEqual(
            self.live(identifier, reasoning=True),
            {
                "phase": job["stage"],
                "phase_code": job["phase_code"],
                "phase_args": job["phase_args"],
                "content": "",
                "reasoning": "",
                "active": False,
                "enabled": True,
            },
        )

    def test_terminal_legacy_previews_remain_readable_without_attempts_or_snapshots(
        self,
    ):
        for status in ("done", "failed", "cancelled"):
            identifier = self.identifiers[status]
            with self.subTest(status=status):
                self.assertIsNone(self.store.get_live_job(identifier)[1])
                self.assertIsNone(self.store.load_config(identifier))
                self.assertEqual(self.store.attempt_count(identifier), 0)
                public = self.live(identifier)
                self.assertEqual(
                    public["content"], self.payloads[identifier]["content"]
                )
                self.assertEqual(public["reasoning"], "")
                self.assertFalse(public["active"])
                self.assertEqual(
                    self.live(identifier, True)["reasoning"],
                    self.payloads[identifier]["reasoning"],
                )
        self.assertEqual(self.files(), self.files_before)

    def test_queued_legacy_retries_hide_both_disk_and_cached_previews_without_creating_attempts(
        self,
    ):
        for status in ("failed", "cancelled"):
            identifier = self.identifiers[status]
            with self.subTest(status=status):
                self.store.previews.put(identifier, self.payloads[identifier], 1000)
                self.retry(identifier)
                self.assertIsNone(self.store.previews.get(identifier))
                self.assertIsNone(self.store.get_live_job(identifier)[1])
                self.assert_empty(identifier)
                # A separate publisher/reader may still have an untagged cached snapshot.
                self.store.previews.put(identifier, self.payloads[identifier], 1000)
                self.assert_empty(identifier)
                self.assertEqual(self.store.attempt_count(identifier), 0)
                self.assertIsNone(self.store.load_config(identifier))
                done = self.identifiers["done"]
                self.assertEqual(
                    self.live(done)["content"], self.payloads[done]["content"]
                )
        self.assertEqual(self.files(), self.files_before)

    def test_restart_and_claim_hide_interrupted_legacy_output_before_any_attempt_exists(
        self,
    ):
        identifier = self.identifiers["running"]
        self.assertEqual(self.store.get_job(identifier)["status"], "queued")
        self.assert_empty(identifier)
        self.assertEqual(self.store.claim_next_job(), identifier)
        self.assert_empty(identifier)
        self.store.initialize()
        self.assertEqual(self.store.get_job(identifier)["status"], "queued")
        self.assertIsNone(self.store.get_live_job(identifier)[1])
        self.assert_empty(identifier)
        self.assertEqual(self.files(), self.files_before)

    def test_new_attempt_accepts_current_memory_and_disk_previews_but_rejects_untagged_output(
        self,
    ):
        identifier = self.identifiers["failed"]
        self.retry(identifier)
        self.assertEqual(self.store.claim_next_job(), identifier)
        recorder = self.store.start_attempt(
            identifier, self.store.get_job(identifier)["queued_at"]
        )
        self.store.update_job(
            identifier, stage="Analyzing section 1/1", phase_code="section"
        )
        self.assert_empty(identifier)  # The old untagged disk file is still intact.
        current = {
            "_attempt_id": recorder.run_id,
            "content": "CURRENT_TEXT",
            "reasoning": "CURRENT_REASONING",
            "reasoning_chars": 17,
            "active": True,
        }
        self.store.previews.put(identifier, current, 1000)
        self.assertEqual(self.live(identifier)["content"], "CURRENT_TEXT")
        self.assertEqual(self.live(identifier)["reasoning"], "")
        self.assertTrue(self.live(identifier)["active"])
        self.assertEqual(self.live(identifier, True)["reasoning"], "CURRENT_REASONING")
        self.assertEqual(self.files(), self.files_before)
        path = self.store.job_dir(identifier) / "live_preview.json"
        atomic_json(path, current)
        self.store.previews.clear()
        self.assertEqual(self.live(identifier)["content"], "CURRENT_TEXT")
        expected = dict(self.files_before)
        expected[path.relative_to(self.store.data_dir)] = path.read_bytes()
        self.assertEqual(self.files(), expected)

    def test_error_cancellation_and_restart_preserve_current_partial_output_and_retry_files(
        self,
    ):
        for status in ("failed", "cancelled"):
            identifier = self.identifiers[status]
            with self.subTest(status=status):
                self.retry(identifier)
                self.assertEqual(self.store.claim_next_job(), identifier)
                recorder = self.store.start_attempt(
                    identifier, self.store.get_job(identifier)["queued_at"]
                )
                path = self.store.job_dir(identifier) / "live_preview.json"
                payload = {
                    "_attempt_id": recorder.run_id,
                    "content": f"CURRENT_{status}_PARTIAL",
                    "reasoning": "CURRENT_REASONING",
                    "reasoning_chars": 17,
                    "active": True,
                }
                atomic_json(path, payload)
                recorder.finish(status)
                self.store.update_job(
                    identifier,
                    status=status,
                    phase_code=status,
                    error_code="output_limit" if status == "failed" else "cancelled",
                )
                self.assertEqual(self.live(identifier)["content"], payload["content"])
                self.assertFalse(self.live(identifier)["active"])
                before_restart = self.files()
                self.store.initialize()
                self.assertEqual(self.live(identifier)["content"], payload["content"])
                self.assertFalse(self.live(identifier)["active"])
                self.retry(identifier)
                self.assert_empty(identifier)
                self.store.request_cancel(identifier)
                self.assertEqual(self.files(), before_restart)
                retained = self.files()
                for path, contents in self.files_before.items():
                    if path.name != "live_preview.json":
                        self.assertEqual(retained[path], contents)

    def test_mismatched_job_or_attempt_previews_cannot_expose_previous_output(self):
        identifier = self.identifiers["failed"]
        self.retry(identifier)
        self.assertEqual(self.store.claim_next_job(), identifier)
        first = self.store.start_attempt(
            identifier, self.store.get_job(identifier)["queued_at"]
        )
        old = {
            "_attempt_id": first.run_id,
            "content": "PREVIOUS_ATTEMPT",
            "active": True,
        }
        atomic_json(self.store.job_dir(identifier) / "live_preview.json", old)
        first.finish("failed")
        self.store.update_job(identifier, status="failed")
        self.retry(identifier)
        self.assertEqual(self.store.claim_next_job(), identifier)
        current = self.store.start_attempt(
            identifier, self.store.get_job(identifier)["queued_at"]
        )
        self.assertNotEqual(first.run_id, current.run_id)
        self.store.update_job(
            identifier, stage="Analyzing section 1/1", phase_code="section"
        )
        self.store.previews.put(identifier, old, 1000)
        self.store.previews.put(
            self.identifiers["done"],
            {"_attempt_id": current.run_id, "content": "OTHER_JOB", "active": True},
            1000,
        )
        before = self.files()
        self.assert_empty(identifier)
        self.assertEqual(self.files(), before)


class PreviewCacheTests(unittest.TestCase):
    def test_bounds_copies_and_expiration_are_independent_of_reads(self):
        cache = previews.PreviewCache(max_entries=2, ttl=5)
        payload = {
            "content": "abcdefgh",
            "reasoning": "12345678",
            "content_chars": 8,
            "metrics": {"quality_checks": {"warnings": 1}},
            "private_prompt": "SECRET",
        }
        with patch.object(previews.time, "monotonic", return_value=100) as clock:
            cache.put("a", payload, 4)
            payload["metrics"]["quality_checks"]["warnings"] = 99
            first = cache.get("a")
            self.assertEqual(first["content"], "efgh")
            self.assertEqual(first["reasoning"], "5678")
            self.assertEqual(first["content_chars"], 8)
            self.assertNotIn("private_prompt", first)
            first["reasoning"] = ""
            first["metrics"]["quality_checks"]["warnings"] = 42
            self.assertEqual(cache.get("a")["metrics"]["quality_checks"]["warnings"], 1)
            self.assertEqual(cache.get("a")["reasoning"], "5678")
            clock.return_value = 104
            cache.get("a")
            clock.return_value = 105
            self.assertIsNone(cache.get("a"))
            cache.put("a", payload, 4)
            cache.put("b", payload, 4)
            cache.get("a")  # Polling must not protect old publications from eviction.
            cache.put("c", payload, 4)
            self.assertIsNone(cache.get("a"))
            self.assertIsNotNone(cache.get("b"))
            cache.discard("b")
            self.assertIsNone(cache.get("b"))
            cache.clear()
            self.assertIsNone(cache.get("c"))

    def test_concurrent_publishers_and_readers_observe_whole_snapshots(self):
        cache = previews.PreviewCache(max_entries=3)
        gate = threading.Barrier(6)
        for number in range(3):
            cache.put(
                str(number),
                {"content": "0", "content_chars": 0, "metrics": {"output_tokens": 0}},
                16,
            )

        def publish(identifier):
            gate.wait(timeout=5)
            for number in range(200):
                cache.put(
                    identifier,
                    {
                        "content": str(number),
                        "content_chars": number,
                        "metrics": {"output_tokens": number},
                    },
                    16,
                )

        def read(identifier):
            gate.wait(timeout=5)
            for _ in range(300):
                payload = cache.get(identifier)
                if payload:
                    self.assertEqual(int(payload["content"]), payload["content_chars"])
                    self.assertEqual(
                        payload["metrics"]["output_tokens"], payload["content_chars"]
                    )
                    payload["metrics"]["output_tokens"] = -1

        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = [
                pool.submit(action, str(number))
                for number in range(3)
                for action in (publish, read)
            ]
            for task in tasks:
                task.result(timeout=10)


class HybridPreviewTests(unittest.TestCase):
    """Exercise the pipeline writer, SQLite attempt identity and real live route together."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.store.initialize()
        changed = patch.object(app, "store", self.store)
        changed.start()
        self.addCleanup(changed.stop)
        enabled = patch.object(app, "LIVE_OUTPUT_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.client = app.app.test_client()
        self.identifier = "a" * 32
        self.directory = self.store.job_dir(self.identifier)
        self.directory.mkdir(parents=True)
        source = self.directory / "source.pdf"
        source.write_bytes(b"%PDF-test")
        with patch.object(app, "read_system_prompt", return_value="PRIVATE_PROMPT"):
            self.config = app.new_snapshot({"profile": "low"})
        self.config["source_hash"] = file_hash(source)
        self.store.create_job(self.identifier, "test.pdf", "Analyze", 1, self.config)
        self.store.update_job(
            self.identifier,
            status="running",
            phase_code="section",
            stage="Analyzing section 1/1",
            phase_args=json.dumps({"index": 1, "total": 1}),
        )
        self.model = Mock(
            tokenization_calls=0,
            tokenization_ms=0,
            tokenization_cache_hits=0,
            capabilities={},
        )
        self.model.current_metrics = {}
        runtime = pipeline.Runtime(
            self.store, Mock(), Mock(return_value=self.model), Mock(), True, 8
        )
        self.run = pipeline.JobRun(self.identifier, runtime)
        self.run.phase_args = {"index": 1, "total": 1}
        self.run.evidence = reliability.build_evidence(
            [{"page": 1, "text": "Source text"}], 1, [], []
        )
        self.path = self.directory / "live_preview.json"
        self.clock = 100.0
        timer = patch.object(pipeline.time, "monotonic", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        self.run.recorder.started = self.clock

    def live(self, reasoning=False):
        response = self.client.get(
            f"/api/jobs/{self.identifier}/live"
            + ("?include_reasoning=true" if reasoning else "")
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def publish(self, content, *, active=True, force=False):
        self.run._write_preview(
            "section",
            force=force,
            content=content,
            reasoning="SECRET_REASONING",
            content_chars=len(content),
            reasoning_chars=16,
            active=active,
            started_at="2026-11-05T12:00:00+00:00",
            elapsed_seconds=1,
            metrics={"output_tokens": 12},
        )

    def stream(self, *args, on_update):
        """Emit 150 refreshes over 120 simulated seconds, then a terminal callback."""
        for index in range(1, 151):
            self.clock = 100 + index * 0.8
            content = "Scientific answer é. " * (index * 8)
            reasoning = "Emitted reasoning. " * (index * 4)
            self.model.current_metrics = {
                "content_chars": len(content),
                "reasoning_chars": len(reasoning),
                "output_tokens": index,
            }
            on_update(content, reasoning, False)
        on_update(content, reasoning, True)
        return content

    def test_long_generation_keeps_refresh_cadence_with_eight_disk_writes(self):
        self.model.complete.side_effect = self.stream
        with (
            patch.object(pipeline, "atomic_json", wraps=atomic_json) as writes,
            patch.object(
                self.store.previews, "put", wraps=self.store.previews.put
            ) as publications,
        ):
            answer = self.run._generate("section", "instructions", "source", 100, [1])
        self.assertEqual(publications.call_count, 152)  # start, 150 refreshes, end
        self.assertEqual(writes.call_count, 8)  # start, six 20-second checkpoints, end
        self.assertEqual(self.live()["content"], answer[-8:])
        self.assertFalse(self.live()["active"])

    def test_frequent_updates_use_memory_and_checkpoint_only_at_boundaries(self):
        with patch.object(pipeline, "atomic_json", wraps=atomic_json) as writes:
            self.publish("")
            for index in range(1, 25):
                self.clock = 100 + index * 0.8
                self.publish(f"Answer {index:02d}")
                self.assertEqual(self.live()["content"], f"Answer {index:02d}"[-8:])
            self.assertEqual(writes.call_count, 1)
            self.assertEqual(json.loads(self.path.read_text())["content"], "")
            self.clock = 120
            self.publish("checkpoint")
            self.assertEqual(writes.call_count, 2)
            self.assertEqual(json.loads(self.path.read_text())["content"], "eckpoint")
            self.clock = 120.8
            self.publish("final answer", active=False)
            self.assertEqual(writes.call_count, 3)
            self.assertFalse(self.live()["active"])
            self.assertEqual(json.loads(self.path.read_text())["content"], "l answer")

    def test_cache_preferred_and_reasoning_redaction_never_mutates_it(self):
        self.publish("first")
        self.publish("latest content")
        with patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("Cache hit must not read JSON"),
        ):
            public = self.live()
            private = self.live(reasoning=True)
        self.assertEqual(public["content"], " content")
        self.assertEqual(public["reasoning"], "")
        self.assertTrue(public["reasoning_available"])
        self.assertEqual(private["reasoning"], "EASONING")
        self.assertTrue(private["active"])
        self.assertEqual(private["metrics"], {"output_tokens": 12})
        self.assertEqual(private["phase_args"], {"index": 1, "total": 1})
        self.assertNotIn("_attempt_id", private)
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(private))

    def test_empty_cache_legacy_malformed_and_missing_disk_fallbacks(self):
        self.assertEqual(self.live()["content"], "")
        self.run.recorder.finish("done")
        self.store.update_job(self.identifier, status="done")
        for raw in ("not json", "[]", "null", "\udcff"):
            self.path.write_bytes(raw.encode("utf-8", errors="surrogateescape"))
            self.assertEqual(self.live()["content"], "")
        atomic_json(
            self.path,
            {
                "content": "legacy",
                "reasoning": "SECRET",
                "reasoning_chars": 6,
                "active": True,
            },
        )
        self.assertEqual(self.live()["content"], "legacy")
        self.assertFalse(self.live()["active"])
        self.assertEqual(self.live(True)["reasoning"], "SECRET")

    def test_separate_process_store_sees_checkpoints_without_caching_disk(self):
        self.publish("checkpoint")
        other_store = JobStore(self.store.data_dir)
        self.addCleanup(other_store.close)
        with patch.object(app, "store", other_store):
            self.assertEqual(self.live()["content"], "eckpoint")
            self.publish("memory only")
            self.assertEqual(self.live()["content"], "eckpoint")
            self.clock = 120
            self.publish("fresh")
            self.assertEqual(self.live()["content"], "fresh")
            self.publish("finished", active=False)
            self.assertEqual(self.live()["content"], "finished")
            self.assertFalse(self.live()["active"])
        self.assertIsNone(other_store.previews.get(self.identifier))

    def test_restart_recovery_and_claim_never_activate_old_output(self):
        self.publish("old pass")
        self.store.initialize()
        self.assertIsNone(self.store.previews.get(self.identifier))
        self.assertEqual(self.live()["content"], "")
        self.assertEqual(self.store.claim_next_job(), self.identifier)
        self.assertEqual(self.live()["content"], "")
        self.store.start_attempt(
            self.identifier, app.store.get_job(self.identifier)["queued_at"]
        )
        self.store.update_job(
            self.identifier, stage="Preparing document", phase_code="preparing"
        )
        self.assertEqual(self.live()["content"], "")

    def test_retry_discards_cache_and_rejects_old_disk_then_accepts_new_attempt(self):
        self.publish("old pass")
        old = self.store.previews.get(self.identifier)
        self.run.recorder.finish("failed")
        self.store.update_job(self.identifier, status="failed")
        with patch.object(app.worker, "wake"):
            self.assertEqual(
                self.client.post(f"/api/jobs/{self.identifier}/retry").status_code, 200
            )
        self.assertIsNone(self.store.previews.get(self.identifier))
        self.assertEqual(self.live()["content"], "")
        self.store.claim_next_job()
        self.assertEqual(self.live()["content"], "")
        self.run = pipeline.JobRun(self.identifier, self.run.runtime)
        self.store.update_job(
            self.identifier, phase_code="section", stage="Analyzing section 1/1"
        )
        self.publish("new pass")
        # Simulate another process retaining an old cached snapshot across the retry.
        self.store.previews.put(self.identifier, old, 8)
        self.assertEqual(self.live()["content"], "new pass")
        self.assertTrue(self.live()["active"])

    def test_generation_failure_cancellation_and_shutdown_flush_partial_text(self):
        for error in (
            ModelError("stream_error", "Unavailable"),
            Cancelled(),
            WorkerStopping(),
        ):
            with self.subTest(error=type(error).__name__):

                def stream(*args, on_update):
                    self.model.current_metrics = {
                        "content_chars": 18,
                        "reasoning_chars": 9,
                        "output_tokens": 4,
                    }
                    on_update("useful partial text", "reasoning", False)
                    raise error

                self.model.complete.side_effect = stream
                with self.assertRaises(type(error)) as raised:
                    self.run._generate("section", "instructions", "source", 100, [1])
                self.assertIs(raised.exception, error)
                disk = json.loads(self.path.read_text())
                self.assertEqual(disk["content"], "ial text")
                self.assertEqual(disk["reasoning"], "easoning")
                self.assertFalse(disk["active"])
                self.assertEqual(disk["metrics"]["output_tokens"], 4)

    def test_generation_end_and_attempt_end_persist_then_release_cache(self):
        def stream(*args, on_update):
            on_update("final answer", "reasoning", True)
            return "final answer"

        self.model.complete.side_effect = stream
        self.run._prepare = Mock()
        self.run._extract = Mock(
            return_value={"page_count": 1, "missing": [], "ocr_pages": []}
        )
        self.run._split = Mock(return_value=[{}])
        self.run._analyze_sections = Mock(return_value=[])
        self.run._merge_until_final_fits = Mock(return_value=([], "source", None))
        self.run._write_final = lambda *args: self.run._generate(
            "final", "instructions", "source", 100, [1]
        )
        self.run._save_report = lambda *args: self.store.update_job(
            self.identifier, status="done"
        )
        self.run.execute()
        self.assertIsNone(self.store.previews.get(self.identifier))
        self.assertEqual(self.live()["content"], "l answer")
        self.assertFalse(self.live()["active"])
        self.assertFalse(json.loads(self.path.read_text())["active"])

    def test_failed_checkpoint_keeps_the_original_generation_error(self):
        error = ModelError("stream_error", "PRIVATE_REMOTE_MESSAGE")

        def stream(*args, on_update):
            on_update("partial", "reasoning", False)
            raise error

        def persist(path, payload):
            if not payload["active"]:
                raise OSError("PRIVATE_PATH")
            atomic_json(path, payload)

        self.model.complete.side_effect = stream
        with (
            patch.object(pipeline, "atomic_json", side_effect=persist),
            self.assertLogs(app.log, level="ERROR") as logs,
            self.assertRaises(ModelError) as raised,
        ):
            self.run._generate("section", "instructions", "source", 100, [1])
        self.assertIs(raised.exception, error)
        self.assertNotIn("PRIVATE_REMOTE_MESSAGE", "\n".join(logs.output))
        self.assertNotIn("PRIVATE_PATH", "\n".join(logs.output))
        self.assertEqual(self.live()["content"], "partial")
        self.assertFalse(self.live()["active"])

    def test_terminal_status_delete_and_disabled_live_output(self):
        self.publish("useful")
        for status in ("failed", "cancelled", "done"):
            self.store.update_job(self.identifier, status=status)
            self.assertEqual(self.live()["content"], "useful")
            self.assertFalse(self.live()["active"])
        self.run.runtime = replace(self.run.runtime, live_output=False)
        with (
            patch.object(app, "LIVE_OUTPUT_ENABLED", False),
            patch.object(
                pipeline,
                "atomic_json",
                side_effect=AssertionError("Live output is disabled"),
            ),
            patch.object(
                self.store.previews,
                "put",
                side_effect=AssertionError("Live output is disabled"),
            ),
        ):
            self.run._write_preview("final", content="not cached")
            self.assertEqual(self.live()["content"], "")
            self.assertFalse(self.live()["enabled"])

            def stream(*args, on_update):
                on_update("answer", "reasoning", True)
                return "answer"

            self.model.complete.side_effect = stream
            self.assertEqual(
                self.run._generate("final", "instructions", "source", 100, [1]),
                "answer",
            )
        self.run.recorder.finish("done")
        self.assertEqual(
            self.client.post(f"/api/jobs/{self.identifier}/delete").status_code, 200
        )
        self.assertIsNone(self.store.previews.get(self.identifier))
        self.assertFalse(self.path.exists())
        self.assertEqual(
            self.client.get(f"/api/jobs/{self.identifier}/live").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
