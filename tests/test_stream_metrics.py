"""Check streaming failures, optional measurements and silent-prefill cancellation."""

import json
import threading
import time
import unittest
from unittest.mock import patch

import app
from document_processor.model_client import Cancelled, ModelError, ThinkingSplitter, collect_metrics


class Stream:
    """Expose arbitrary SSE lines, including malformed and interrupted streams."""

    def __init__(self, events, done=True):
        self.events, self.done = events, done

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def iter_lines(self, **kwargs):
        for event in self.events:
            yield event if isinstance(event, bytes) else ("data: " + json.dumps(event)).encode()
        if self.done:
            yield b"data: [DONE]"


def delta(text, finish=None, reasoning=None):
    """Build a minimal compatible event without making one event equal one token."""
    return {"choices": [{"delta": {"content": text, "reasoning_content": reasoning}, "finish_reason": finish}]}


class StreamTests(unittest.TestCase):
    def client(self, **overrides):
        """Build a client with explicit fake credentials and a fixed private prompt."""
        with patch.object(app, "read_system_prompt", return_value="PRIVATE_PROMPT"):
            client = app.ModelClient()
        client.config.update(overrides)
        self.addCleanup(client.session.close)
        return client

    def complete(self, client, stream):
        """Replace all model I/O while exercising the actual parser and reader thread."""
        with patch.object(client, "tokenize", return_value=10), \
             patch.object(client, "model", return_value="fake"), \
             patch.object(client.session, "post", return_value=stream):
            return client.complete("Analyze", "Source", 100)

    def test_usage_only_final_event_is_persisted(self):
        client = self.client()
        events = [delta("Answer", "stop"), {"choices": [], "usage": {
            "prompt_tokens": 40, "completion_tokens": 12,
            "completion_tokens_details": {"reasoning_tokens": 4},
            "prompt_tokens_details": {"cached_tokens": 20}},
            "timings": {"prompt_n": 20, "prompt_ms": 1000, "predicted_n": 12, "predicted_ms": 6000}}]
        self.assertEqual(self.complete(client, Stream(events)), "Answer")
        metrics = client.last_result.metrics
        self.assertEqual(metrics["output_tokens"], 12)
        self.assertEqual(metrics["useful_tokens"], 8)
        self.assertEqual(metrics["cached_prompt_tokens"], 20)
        self.assertEqual(metrics["decode_tokens_per_second"], 2)
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(metrics))

    def test_missing_usage_is_unknown_and_done_does_not_prove_eos(self):
        client = self.client()
        self.complete(client, Stream([delta("Many words in one event")]))
        self.assertIsNone(client.current_metrics["output_tokens"])
        self.assertIsNone(client.current_metrics["prefill_ms"])
        self.assertEqual(client.current_metrics["stop_category"], "done_without_reason")

    def test_malformed_empty_cutoff_and_length_streams_are_not_committed(self):
        cases = [Stream([b"data: {invalid"]), Stream([delta("Partial")], done=False),
                 Stream([delta("", "stop")]), Stream([delta("Partial", "length")]),
                 Stream([{"error": "PRIVATE_REMOTE_BODY"}]), Stream([delta("Text", "tool_calls")]),
                 Stream([{"choices": "not a list"}])]
        for stream in cases:
            client = self.client()
            with self.subTest(events=stream.events), self.assertRaises(ModelError) as error:
                self.complete(client, stream)
            self.assertIsNone(client.last_result)
            self.assertIsNotNone(client.current_metrics["request_ms"])
            self.assertNotIn("PRIVATE_REMOTE_BODY", str(error.exception))
        self.assertEqual(client.current_metrics["stop_category"], "error")

    def test_length_reason_is_retained_on_failure(self):
        client = self.client()
        with self.assertRaises(ModelError):
            self.complete(client, Stream([delta("Partial", "length")]))
        self.assertEqual(client.current_metrics["finish_reason"], "length")

    def test_template_opening_is_handled_only_when_explicitly_configured(self):
        client = self.client(initial_thinking=True)
        self.assertEqual(self.complete(client, Stream([delta("reasoning</think>Useful", "stop")])), "Useful")
        parser = ThinkingSplitter(initial_thinking=True, reasoning_limit=8)
        parser.feed("a" * 100 + "</think>Facts [Page 2]")
        parser.finish()
        self.assertEqual(parser.reasoning_chars, 100)
        self.assertEqual(len(parser.reasoning), 8)
        self.assertEqual(parser.content, "Facts [Page 2]")

    def test_server_separated_reasoning_takes_priority_over_initial_template_state(self):
        client = self.client(initial_thinking=True)
        self.assertEqual(self.complete(client, Stream([
            delta("", reasoning="already separated"), delta("Useful", "stop")])), "Useful")

    def test_verified_thinking_adapter_sends_stage_specific_option(self):
        client = self.client(thinking_adapter="chat_template", thinking_section="off", thinking_final="on")
        with patch.object(client, "tokenize", return_value=10), patch.object(client, "model", return_value="fake"), \
             patch.object(client.session, "post", return_value=Stream([delta("Answer", "stop")])) as post:
            client.complete("Analyze", "Source", 100)
            self.assertEqual(post.call_args.kwargs["json"]["chat_template_kwargs"], {"enable_thinking": False})
        client.current_stage = "final"
        with patch.object(client, "tokenize", return_value=10), patch.object(client, "model", return_value="fake"), \
             patch.object(client.session, "post", return_value=Stream([delta("Answer", "stop")])) as post:
            client.complete("Analyze", "Source", 100)
            self.assertEqual(post.call_args.kwargs["json"]["chat_template_kwargs"], {"enable_thinking": True})

    def test_silent_stream_has_a_real_deadline_and_can_be_cancelled(self):
        for mode in ("deadline", "cancel"):
            release = threading.Event()

            class Silent(Stream):
                def iter_lines(self, **kwargs):
                    release.wait(3)
                    if False:
                        yield None

                def close(self):
                    release.set()

            client = self.client(step_timeout_seconds=0.05 if mode == "deadline" else 5)
            start = time.monotonic()
            client.cancel_check = lambda: mode == "cancel" and time.monotonic() - start > 0.05
            try:
                with self.assertRaises(Cancelled if mode == "cancel" else ModelError):
                    self.complete(client, Silent([]))
                self.assertLess(time.monotonic() - start, 2)
            finally:
                release.set()

    def test_tokenization_cache_is_bounded_and_counts_hits(self):
        client = self.client()
        with patch.object(client, "json_request", return_value={"tokens": [1, 2, 3]}) as request:
            self.assertEqual(client.tokenize("identical"), 3)
            self.assertEqual(client.tokenize("identical"), 3)
            request.assert_called_once()
        self.assertEqual(client.tokenization_cache_hits, 1)

    def test_negative_and_nonfinite_measurements_are_ignored(self):
        metrics = {}
        collect_metrics({"usage": {"prompt_tokens": -1, "completion_tokens": float("inf")}}, metrics)
        self.assertEqual(metrics, {})


if __name__ == "__main__":
    unittest.main()
