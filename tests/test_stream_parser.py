"""Deterministic parser sequences and the boundary with HTTP/artifact storage."""

import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from document_processor import model_client
from document_processor.config import ConfigurationError
from document_processor.errors import ModelError
from document_processor.model_client import ModelClient, StreamParser
from document_processor.pipeline import JobRun
from document_processor.settings import Settings


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False)).encode("utf-8")


def delta(content="", *, finish=None, **channels):
    return {
        "choices": [
            {"delta": {"content": content, **channels}, "finish_reason": finish}
        ]
    }


class StreamParserTests(unittest.TestCase):
    def test_lines_utf8_done_and_validated_state(self):
        parser = StreamParser()
        for line in (None, b"", "", ": keepalive", b"event: message"):
            self.assertFalse(parser.feed(line))
        self.assertTrue(parser.feed(sse(delta("  Réponse 中 😀  "))))
        self.assertEqual(parser.state.content, "  Réponse 中 😀  ")
        self.assertIs(parser.state.metrics, parser.metrics)
        self.assertFalse(parser.state.completed)
        self.assertFalse(parser.feed("data: [DONE]"))
        self.assertTrue(parser.done)
        self.assertFalse(parser.state.completed)
        self.assertFalse(parser.feed(sse({"error": "ignored after DONE"})))
        self.assertEqual(parser.finish(), "Réponse 中 😀")
        self.assertTrue(parser.state.completed)
        self.assertEqual(parser.metrics["content_chars"], len("  Réponse 中 😀  "))
        self.assertEqual(parser.metrics["stop_category"], "done_without_reason")

    def test_fragmented_think_tags_and_bounded_reasoning_keep_total_counts(self):
        text = "Avant<think>réflexion 中</think>Après<thi"
        for width in range(1, 15):
            with self.subTest(width=width):
                parser = StreamParser(preview_limit=7)
                for start in range(0, len(text), width):
                    parser.feed(sse(delta(text[start : start + width])))
                parser.feed(sse(delta(reasoning="canal séparé")))
                parser.feed(b"data: [DONE]")
                self.assertEqual(parser.finish(), "AvantAprès<thi")
                state = parser.state
                self.assertEqual(state.reasoning, "canal séparé"[-7:])
                self.assertEqual(state.metrics["content_chars"], len("AvantAprès<thi"))
                self.assertEqual(
                    state.metrics["reasoning_chars"], len("réflexion 中canal séparé")
                )

    def test_initial_template_state_and_separated_channel_precedence(self):
        cases = (
            ("inherit", delta("réflexion</think>Réponse"), "Réponse", "réflexion"),
            ("off", delta("Réponse"), "Réponse", ""),
            (
                "inherit",
                delta("Réponse", reasoning_content="séparé"),
                "Réponse",
                "séparé",
            ),
            ("off", delta("Réponse", reasoning="séparé"), "Réponse", "séparé"),
        )
        for policy, event, answer, reasoning in cases:
            with self.subTest(policy=policy, event=event):
                parser = StreamParser(initial_thinking=True, thinking_policy=policy)
                parser.feed(sse(event))
                parser.feed(b"data: [DONE]")
                self.assertEqual(parser.finish(), answer)
                self.assertEqual(parser.state.reasoning, reasoning)

    def test_usage_after_stop_and_timing_use_injected_clock(self):
        ticks = iter((5.25, 5.5))
        parser = StreamParser(started=5.0, clock=lambda: next(ticks))
        parser.feed(sse(delta("Réponse", finish="stop")))
        self.assertFalse(parser.done)
        self.assertFalse(parser.state.completed)
        parser.feed(
            sse(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 12,
                        "completion_tokens_details": {"reasoning_tokens": 4},
                        "prompt_tokens_details": {"cached_tokens": 20},
                    },
                    "timings": {
                        "prompt_n": 20,
                        "prompt_ms": 1000,
                        "predicted_ms": 6000,
                    },
                }
            )
        )
        self.assertEqual(parser.finish(), "Réponse")
        metrics = parser.state.metrics
        for name, expected in {
            "first_event_ms": 250,
            "first_content_ms": 500,
            "prompt_tokens": 40,
            "output_tokens": 12,
            "reasoning_tokens": 4,
            "useful_tokens": 8,
            "cached_prompt_tokens": 20,
            "prefill_tokens_per_second": 20,
            "decode_tokens_per_second": 2,
            "finish_reason": "stop",
            "stop_category": "stop",
        }.items():
            self.assertEqual(metrics[name], expected, name)

    def test_missing_metrics_stay_unknown_and_invalid_values_do_not_replace_counts(
        self,
    ):
        parser = StreamParser()
        parser.feed(sse(delta("Many words are still not a token measurement")))
        self.assertIsNone(parser.metrics["output_tokens"])
        self.assertIsNone(parser.metrics["reasoning_tokens"])
        parser.feed(sse({"timings": {"predicted_n": 8, "predicted_ms": 2000}}))
        for value in (-1, True, "9", None):
            parser.feed(sse({"usage": {"completion_tokens": value}}))
        self.assertEqual(parser.metrics["output_tokens"], 8)
        self.assertEqual(parser.metrics["output_tokens_source"], "engine")
        self.assertEqual(parser.metrics["decode_tokens_per_second"], 4)

    def test_malformed_frames_keep_public_codes_and_messages(self):
        cases = (
            (
                b"data: \xff",
                "invalid_stream_encoding",
                "The model stream is not valid UTF-8.",
            ),
            (b"data: {", "invalid_stream", "Malformed llama.cpp streaming response."),
            (sse(None), "stream_error", "The backend reported a streaming error."),
            (sse([]), "stream_error", "The backend reported a streaming error."),
            (
                sse({"error": None}),
                "stream_error",
                "The backend reported a streaming error.",
            ),
            (
                sse({"choices": {"bad": 1}}),
                "invalid_stream",
                "Malformed streaming choices.",
            ),
            (sse({"choices": [None]}), "invalid_stream", "Malformed streaming choice."),
            (
                sse({"choices": [{"delta": [1]}]}),
                "invalid_stream",
                "Malformed streaming delta.",
            ),
        )
        for line, code, message in cases:
            with self.subTest(line=line):
                parser = StreamParser()
                with self.assertRaises(ModelError) as caught:
                    parser.feed(line)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(parser.state.completed)

    def test_falsy_optional_structures_keep_existing_fallbacks(self):
        parser = StreamParser()
        for value in (None, False, 0, "", [], {}):
            parser.feed(sse({"choices": value}))
            parser.feed(sse({"choices": [{"delta": value}]}))
        parser.feed(sse(delta("Answer", finish="stop")))
        self.assertEqual(parser.finish(), "Answer")

    def test_finish_reasons_and_error_precedence(self):
        cases = (
            ("Answer", None, True, None),
            ("Answer", "stop", False, None),
            ("Answer", "stop", True, None),
            ("Answer", None, False, "incomplete_stream"),
            ("", None, False, "incomplete_stream"),
            (" ", None, True, "empty_output"),
            ("", "length", True, "output_limit"),
            ("Answer", "length", False, "output_limit"),
            ("Answer", "tool_calls", True, "unsupported_finish"),
            ("Answer", "content_filter", True, "unsupported_finish"),
            ("Answer", {"unexpected": "reason"}, True, "unsupported_finish"),
            ("Answer", ["stop"], True, "unsupported_finish"),
        )
        for content, reason, done, error in cases:
            with self.subTest(content=content, reason=reason, done=done):
                parser = StreamParser()
                parser.feed(sse(delta(content, finish=reason)))
                if done:
                    parser.feed(b"data: [DONE]")
                if error:
                    with self.assertRaises(ModelError) as caught:
                        parser.finish()
                    self.assertEqual(caught.exception.code, error)
                    self.assertFalse(parser.state.completed)
                else:
                    self.assertEqual(parser.finish(), content)
                    self.assertTrue(parser.state.completed)
                self.assertEqual(
                    parser.metrics["finish_reason"],
                    reason if isinstance(reason, str) else None,
                )

    def test_size_limit_includes_pending_tag_flush(self):
        for suffix, error_at in (("", None), ("x", "feed"), ("<thi", "finish")):
            with self.subTest(suffix=suffix):
                parser = StreamParser()
                line = sse(delta("x" * 4_000_000 + suffix, finish="stop"))
                if error_at == "feed":
                    with self.assertRaises(ModelError) as caught:
                        parser.feed(line)
                else:
                    parser.feed(line)
                    if error_at == "finish":
                        with self.assertRaises(ModelError) as caught:
                            parser.finish()
                    else:
                        self.assertEqual(len(parser.finish()), 4_000_000)
                if error_at:
                    self.assertEqual(caught.exception.code, "output_too_large")
                    self.assertFalse(parser.state.completed)


class SimulatedResponse:
    """A reader which can remain blocked until the adapter closes it."""

    def __init__(self, lines, *, block=False):
        self.lines = lines
        self.block = block
        self.closed = threading.Event()
        self.exited = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited.set()

    def raise_for_status(self):
        pass

    def close(self):
        self.closed.set()

    def iter_lines(self, **kwargs):
        yield from self.lines
        if self.block and not self.closed.wait(2):
            raise AssertionError("Adapter did not close the rejected stream")


class StreamAdapterTests(unittest.TestCase):
    def client(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        client = ModelClient(
            "http://unused.invalid", "", settings.job_defaults(), "PRIVATE"
        )
        self.addCleanup(client.session.close)
        return client

    def test_parser_failure_closes_http_reader_and_releases_gate(self):
        for line, code in (
            (b"data: {", "invalid_stream"),
            (b"data: \xff", "invalid_stream_encoding"),
            (sse({"error": {"message": "PRIVATE"}}), "stream_error"),
        ):
            with self.subTest(code=code):
                client = self.client()
                response = SimulatedResponse([line], block=True)
                self.addCleanup(response.close)
                gate = threading.Lock()
                with (
                    patch.object(model_client, "_stream_gate", gate),
                    patch.object(
                        client, "fits", return_value=(True, 10, "tokenizer", 0)
                    ),
                    patch.object(client, "model", return_value="fake"),
                    patch.object(client.session, "post", return_value=response),
                ):
                    with self.assertRaises(ModelError) as caught:
                        client.generate("Analyze", "Source", 100)
                    self.assertEqual(caught.exception.code, code)
                    self.assertTrue(response.closed.wait(1))
                    self.assertTrue(response.exited.wait(1))
                    self.assertTrue(gate.acquire(timeout=1))
                    gate.release()
                self.assertIsNone(client.last_result)
                self.assertEqual(client.current_metrics["error_code"], code)
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_context_budget_rejection_never_starts_http(self):
        client = self.client()
        with (
            patch.object(client, "fits", return_value=(False, 1000, "tokenizer", 0)),
            patch.object(client.session, "post") as post,
            self.assertRaises(ConfigurationError) as caught,
        ):
            client.generate("Analyze", "Source", 100)
        self.assertEqual(caught.exception.code, "context_insufficient")
        post.assert_not_called()
        self.assertIsNone(client.last_result)

    def test_reported_token_budget_does_not_override_finish_reason(self):
        for reason in ("stop", "length"):
            with self.subTest(reason=reason):
                client = self.client()
                response = SimulatedResponse(
                    [
                        sse(delta("Answer", finish=reason)),
                        sse({"usage": {"completion_tokens": 100}, "choices": []}),
                        b"data: [DONE]",
                    ]
                )
                with (
                    patch.object(
                        client, "fits", return_value=(True, 10, "tokenizer", 0)
                    ),
                    patch.object(client, "model", return_value="fake"),
                    patch.object(client.session, "post", return_value=response) as post,
                ):
                    if reason == "length":
                        with self.assertRaises(ModelError) as caught:
                            client.generate("Analyze", "Source", 100)
                        self.assertEqual(caught.exception.code, "output_limit")
                        self.assertIsNone(client.last_result)
                    else:
                        result = client.generate("Analyze", "Source", 100)
                        self.assertEqual(result.content, "Answer")
                        self.assertIs(result.metrics, client.current_metrics)
                self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 100)
                self.assertEqual(client.current_metrics["output_tokens"], 100)
                self.assertEqual(client.current_metrics["output_budget"], 100)

    def test_partial_generations_never_reach_artifact_storage(self):
        cases = (
            ([sse(delta("Partial", finish="length")), b"data: [DONE]"], "output_limit"),
            ([sse(delta("Partial"))], "incomplete_stream"),
            ([sse(delta("Partial")), b"data: {"], "invalid_stream"),
        )
        for lines, code in cases:
            with self.subTest(code=code):
                client = self.client()
                run = JobRun.__new__(JobRun)
                run.model = client
                run.recorder = Mock()
                run.artifacts = Mock()
                run.artifacts.read.return_value = None
                response = SimulatedResponse(lines)
                with (
                    patch.object(
                        client, "fits", return_value=(True, 10, "tokenizer", 0)
                    ),
                    patch.object(client, "model", return_value="fake"),
                    patch.object(client.session, "post", return_value=response),
                    self.assertRaises(ModelError) as caught,
                ):
                    run._step(
                        "rapport_final.md",
                        "final",
                        {},
                        lambda client=client: client.complete("Analyze", "Source", 100),
                    )
                self.assertEqual(caught.exception.code, code)
                run.artifacts.write.assert_not_called()
                self.assertIsNone(client.last_result)
                self.assertEqual(run.recorder.end.call_args.args[1], "failed")


if __name__ == "__main__":
    unittest.main()
