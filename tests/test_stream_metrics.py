"""Check streaming failures, optional measurements and silent-prefill cancellation."""

import itertools
import json
import threading
import time
import unittest
from unittest.mock import patch

from support import SSEStream as Stream

import app
from document_processor import model_client
from document_processor.errors import Cancelled, ModelError, WorkerStopping
from document_processor.model_client import ThinkingSplitter, collect_metrics


def delta(text, finish=None, reasoning=None):
    """Build a minimal compatible event without making one event equal one token."""
    return {
        "choices": [
            {
                "delta": {"content": text, "reasoning_content": reasoning},
                "finish_reason": finish,
            }
        ]
    }


class CountingParts(list):
    """Measure visits to stored fragments instead of asserting a wall-clock duration."""

    def __init__(self):
        super().__init__()
        self.visits = 0

    def __iter__(self):
        for part in super().__iter__():
            self.visits += 1
            yield part


class SplitterCountTests(unittest.TestCase):
    def test_fragmented_tags_and_unicode_counts_match_emitted_channels(self):
        text = "Avant é👩🏽‍🔬 <think>réflexion 中</think>Après<think>suite</think> fin"
        for width in range(1, 15):
            for limit in (None, 7):
                with self.subTest(width=width, limit=limit):
                    splitter = ThinkingSplitter(reasoning_limit=limit)
                    for start in range(0, len(text), width):
                        splitter.feed(text[start : start + width])
                        self.assertEqual(splitter.content_chars, len(splitter.content))
                    splitter.finish()
                    self.assertEqual(splitter.content, "Avant é👩🏽‍🔬 Après fin")
                    self.assertEqual(
                        splitter.content_chars, len("Avant é👩🏽‍🔬 Après fin")
                    )
                    self.assertEqual(splitter.reasoning_chars, len("réflexion 中suite"))
                    self.assertEqual(
                        splitter.reasoning,
                        "réflexion 中suite"
                        if limit is None
                        else "réflexion 中suite"[-limit:],
                    )

    def test_initial_reasoning_and_incomplete_tags_are_counted_only_when_emitted(self):
        for text, initial, content, reasoning in (
            ("text<thi", False, "text<thi", ""),
            ("before<think>work</thi", False, "before", "work</thi"),
            ("work</think>done<th", True, "done<th", "work"),
        ):
            with self.subTest(text=text):
                splitter = ThinkingSplitter(initial_thinking=initial)
                for character in text:
                    splitter.feed(character)
                    self.assertEqual(splitter.content_chars, len(splitter.content))
                    self.assertEqual(splitter.reasoning_chars, len(splitter.reasoning))
                splitter.finish()
                self.assertEqual(
                    (splitter.content, splitter.reasoning), (content, reasoning)
                )
                self.assertEqual(
                    (splitter.content_chars, splitter.reasoning_chars),
                    (len(content), len(reasoning)),
                )
                splitter.finish()
                self.assertEqual(
                    (splitter.content_chars, splitter.reasoning_chars),
                    (len(content), len(reasoning)),
                )


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
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(client.session, "post", return_value=stream),
        ):
            return client.complete("Analyze", "Source", 100)

    def test_usage_only_final_event_is_persisted(self):
        client = self.client()
        events = [
            delta("Answer", "stop"),
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
                    "predicted_n": 12,
                    "predicted_ms": 6000,
                },
            },
        ]
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

    def test_fragmented_output_lengths_do_not_repeatedly_scan_prior_fragments(self):
        seen = []

        class TrackedSplitter(ThinkingSplitter):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.content_parts = CountingParts()
                seen.append(self)

        count = 5000
        client = self.client()
        with patch.object(model_client, "ThinkingSplitter", TrackedSplitter):
            result = self.complete(client, Stream([delta("é😀")] * count))
        self.assertEqual(result, "é😀" * count)
        self.assertEqual(client.current_metrics["content_chars"], count * 2)
        self.assertEqual(client.current_metrics["reasoning_chars"], 0)
        self.assertLessEqual(
            seen[0].content_parts.visits,
            count * 2,
            "Length accounting must not walk accumulated fragments on every event",
        )

    def test_live_and_final_counts_include_both_reasoning_channels_and_raw_whitespace(
        self,
    ):
        client = self.client()
        client.preview_limit = 7
        events = [
            delta("<thi"),
            delta("nk>réf"),
            delta("lexion</th"),
            delta("ink> Résultat "),
            delta("é ", reasoning="canal "),
            delta("中文", "stop", reasoning="séparé"),
        ]
        previews = []
        clock = itertools.count(1000)

        def update(content, reasoning, done):
            previews.append((content, reasoning, done, dict(client.current_metrics)))

        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(client.session, "post", return_value=Stream(events)),
            patch.object(
                model_client.time, "monotonic", side_effect=lambda: next(clock)
            ),
        ):
            answer = client.complete("Analyze", "Source", 100, on_update=update)
        self.assertEqual(answer, "Résultat é 中文")
        self.assertEqual(
            client.current_metrics["content_chars"], len(" Résultat é 中文")
        )
        self.assertEqual(
            client.current_metrics["reasoning_chars"], len("réflexioncanal séparé")
        )
        self.assertGreater(len(previews), 2)
        for content, reasoning, _, metrics in previews:
            self.assertEqual(metrics["content_chars"], len(content))
            self.assertLessEqual(len(reasoning), 7)
            self.assertGreaterEqual(metrics["reasoning_chars"], len(reasoning))
        self.assertTrue(previews[-1][2])
        self.assertEqual(
            previews[-1][3]["content_chars"], client.current_metrics["content_chars"]
        )
        self.assertEqual(
            previews[-1][3]["reasoning_chars"],
            client.current_metrics["reasoning_chars"],
        )

    def test_four_million_character_limit_including_pending_tag_flush(self):
        for tail, expected_chars, succeeds in (
            ("", 4_000_000, True),
            ("y", 4_000_001, False),
            ("<thi", 4_000_004, False),
        ):
            with self.subTest(tail=tail):
                client = self.client()
                events = [delta("x" * 400000)] * 10
                if tail:
                    events.append(delta(tail))
                if succeeds:
                    self.assertEqual(
                        len(self.complete(client, Stream(events))), expected_chars
                    )
                else:
                    with self.assertRaises(ModelError) as error:
                        self.complete(client, Stream(events))
                    self.assertEqual(error.exception.code, "output_too_large")
                    self.assertIsNone(client.last_result)
                self.assertEqual(
                    client.current_metrics["content_chars"], expected_chars
                )
                self.assertEqual(client.current_metrics["reasoning_chars"], 0)

    def test_cancellation_after_many_fragments_keeps_last_counts(self):
        client = self.client()
        cancelled = threading.Event()
        client.cancel_check = cancelled.is_set
        previews = []
        clock = itertools.count(1000)

        def update(content, reasoning, done):
            previews.append((content, reasoning, done))
            if len(content) >= 4096:
                cancelled.set()

        events = [delta("<think>reason</think>")] + [delta("é" * 256)] * 1000
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(client.session, "post", return_value=Stream(events)),
            patch.object(
                model_client.time, "monotonic", side_effect=lambda: next(clock)
            ),
            self.assertRaises(Cancelled),
        ):
            client.complete("Analyze", "Source", 100, on_update=update)
        self.assertEqual(client.current_metrics["content_chars"], len(previews[-1][0]))
        self.assertEqual(client.current_metrics["reasoning_chars"], len("reason"))
        self.assertEqual(client.current_metrics["stop_category"], "cancelled")
        self.assertIsNone(client.last_result)
        self.assertFalse(previews[-1][2])

    def test_malformed_empty_cutoff_and_length_streams_are_not_committed(self):
        cases = [
            Stream([b"data: {invalid"]),
            Stream([delta("Partial")], done=False),
            Stream([delta("", "stop")]),
            Stream([delta("Partial", "length")]),
            Stream([{"error": "PRIVATE_REMOTE_BODY"}]),
            Stream([delta("Text", "tool_calls")]),
            Stream([{"choices": "not a list"}]),
        ]
        for stream in cases:
            client = self.client()
            with (
                self.subTest(events=stream.events),
                self.assertRaises(ModelError) as error,
            ):
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
        self.assertEqual(
            self.complete(client, Stream([delta("reasoning</think>Useful", "stop")])),
            "Useful",
        )
        self.assertEqual(client.current_metrics["content_chars"], len("Useful"))
        self.assertEqual(client.current_metrics["reasoning_chars"], len("reasoning"))
        parser = ThinkingSplitter(initial_thinking=True, reasoning_limit=8)
        parser.feed("a" * 100 + "</think>Facts [Page 2]")
        parser.finish()
        self.assertEqual(parser.reasoning_chars, 100)
        self.assertEqual(len(parser.reasoning), 8)
        self.assertEqual(parser.content, "Facts [Page 2]")

    def test_server_separated_reasoning_takes_priority_over_initial_template_state(
        self,
    ):
        client = self.client(initial_thinking=True)
        self.assertEqual(
            self.complete(
                client,
                Stream(
                    [delta("", reasoning="already separated"), delta("Useful", "stop")]
                ),
            ),
            "Useful",
        )
        self.assertEqual(client.current_metrics["content_chars"], len("Useful"))
        self.assertEqual(
            client.current_metrics["reasoning_chars"], len("already separated")
        )

    def test_verified_thinking_adapter_sends_stage_specific_option(self):
        client = self.client(
            thinking_adapter="chat_template",
            thinking_section="off",
            thinking_final="on",
        )
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(
                client.session, "post", return_value=Stream([delta("Answer", "stop")])
            ) as post,
        ):
            client.complete("Analyze", "Source", 100)
            self.assertEqual(
                post.call_args.kwargs["json"]["chat_template_kwargs"],
                {"enable_thinking": False},
            )
        client.current_stage = "final"
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(
                client.session, "post", return_value=Stream([delta("Answer", "stop")])
            ) as post,
        ):
            client.complete("Analyze", "Source", 100)
            self.assertEqual(
                post.call_args.kwargs["json"]["chat_template_kwargs"],
                {"enable_thinking": True},
            )

    def test_silent_stream_has_a_real_deadline_and_can_be_cancelled(self):
        # Keep callback signatures; these fixtures are consumed within each iteration.
        for mode in ("deadline", "cancel"):
            release = threading.Event()

            class Silent(Stream):
                def iter_lines(self, **kwargs):
                    release.wait(3)  # noqa: B023
                    if False:
                        yield None

                def close(self):
                    release.set()  # noqa: B023

            client = self.client(step_timeout_seconds=0.05 if mode == "deadline" else 5)
            start = time.monotonic()
            client.cancel_check = lambda: (
                mode == "cancel" and time.monotonic() - start > 0.05  # noqa: B023
            )
            try:
                with self.assertRaises(Cancelled if mode == "cancel" else ModelError):
                    self.complete(client, Silent([]))
                self.assertLess(time.monotonic() - start, 2)
            finally:
                release.set()

    def test_cancellation_from_reader_is_not_reported_as_a_backend_failure(self):
        class CancelledStream(Stream):
            def iter_lines(self, **kwargs):
                yield ("data: " + json.dumps(delta("Partial"))).encode()
                raise Cancelled()

        client = self.client()
        with self.assertRaises(Cancelled):
            self.complete(client, CancelledStream([]))
        self.assertEqual(client.current_metrics["error_code"], "cancelled")
        self.assertEqual(client.current_metrics["stop_category"], "cancelled")
        self.assertIsNone(client.last_result)
        self.assertEqual(
            self.complete(self.client(), Stream([delta("Next answer", "stop")])),
            "Next answer",
        )

    def test_worker_shutdown_during_partial_stream_is_interrupted_without_public_failure(
        self,
    ):
        stopping = threading.Event()
        client = self.client()

        def check():
            if stopping.is_set():
                raise WorkerStopping()
            return False

        client.cancel_check = check
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(
                client.session, "post", return_value=Stream([delta("Partial")])
            ),
            self.assertRaises(WorkerStopping),
        ):
            client.complete(
                "Analyze", "Source", 100, on_update=lambda *args: stopping.set()
            )
        self.assertEqual(client.current_metrics["stop_category"], "interrupted")
        self.assertEqual(client.current_metrics["content_chars"], len("Partial"))
        self.assertEqual(client.current_metrics["reasoning_chars"], 0)
        self.assertNotIn("error_code", client.current_metrics)
        self.assertIsNone(client.last_result)
        self.assertEqual(
            self.complete(self.client(), Stream([delta("Next answer", "stop")])),
            "Next answer",
        )

    def test_cancellation_after_partial_output_closes_the_stream_without_a_result(self):
        client = self.client()
        cancelled, closed = threading.Event(), threading.Event()

        class PartialStream(Stream):
            def iter_lines(self, **kwargs):
                yield ("data: " + json.dumps(delta("Partial"))).encode()
                closed.wait(3)
                yield b"data: [DONE]"

            def close(self):
                closed.set()

        def on_update(content, reasoning, completed):
            if content:
                cancelled.set()

        client.cancel_check = cancelled.is_set
        with (
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(
                client.session, "post", return_value=PartialStream([delta("Partial")])
            ),
            self.assertRaises(Cancelled),
        ):
            client.complete("Analyze", "Source", 100, on_update=on_update)
        self.assertEqual(client.current_metrics["error_code"], "cancelled")
        self.assertEqual(client.current_metrics["stop_category"], "cancelled")
        self.assertIsNone(client.last_result)
        self.assertTrue(
            closed.wait(1), "Cancellation must close the blocked HTTP response"
        )
        self.assertEqual(client.current_metrics["content_chars"], len("Partial"))
        self.assertEqual(client.current_metrics["reasoning_chars"], 0)

    def test_next_generation_waits_for_cleanup_before_headers_without_using_its_deadline(
        self,
    ):
        # Keep callback signatures; these fixtures are consumed within each iteration.
        for mode in ("deadline", "cancel"):
            with (
                self.subTest(mode=mode),
                patch.object(model_client, "_stream_gate", threading.Lock()),
            ):
                gate = model_client._stream_gate
                previous_started = threading.Event()
                release_headers = threading.Event()
                previous_closed = threading.Event()
                previous_body_read = threading.Event()
                following_started = threading.Event()
                following_finished = threading.Event()
                answers, errors = [], []

                class LateResponse(Stream):
                    def __exit__(self, *args):
                        previous_closed.set()  # noqa: B023
                        return False

                    def iter_lines(self, **kwargs):
                        previous_body_read.set()  # noqa: B023
                        yield from super().iter_lines(**kwargs)

                def wait_for_headers(*args, **kwargs):
                    previous_started.set()  # noqa: B023
                    if not release_headers.wait(5):  # noqa: B023
                        raise RuntimeError("Test response headers were not released")
                    return LateResponse([])

                previous = self.client(
                    step_timeout_seconds=0.05 if mode == "deadline" else 5
                )
                previous.cancel_check = lambda: (
                    mode == "cancel" and previous_started.is_set()  # noqa: B023
                )
                following = self.client(step_timeout_seconds=0.2)

                def following_cancel_check():
                    following_started.set()  # noqa: B023
                    return False

                following.cancel_check = following_cancel_check

                def next_response(*args, **kwargs):
                    self.assertTrue(
                        previous_closed.is_set(),  # noqa: B023
                        "Previous HTTP response must close first",
                    )
                    return Stream([delta("Next answer", "stop")])

                def run_following():
                    try:
                        answers.append(following.complete("Analyze", "Source", 100))  # noqa: B023
                    except Exception as exc:
                        errors.append(exc)  # noqa: B023
                    finally:
                        following_finished.set()  # noqa: B023

                thread = threading.Thread(target=run_following)
                with (
                    patch.object(previous, "tokenize", return_value=10),
                    patch.object(previous, "model", return_value="fake"),
                    patch.object(
                        previous.session, "post", side_effect=wait_for_headers
                    ) as previous_post,
                    patch.object(following, "tokenize", return_value=10),
                    patch.object(following, "model", return_value="fake"),
                    patch.object(
                        following.session, "post", side_effect=next_response
                    ) as following_post,
                ):
                    try:
                        with self.assertRaises(
                            Cancelled if mode == "cancel" else ModelError
                        ) as raised:
                            previous.complete("Analyze", "Source", 100)
                        self.assertEqual(
                            raised.exception.code,
                            "cancelled" if mode == "cancel" else "deadline",
                        )
                        self.assertTrue(previous_started.is_set())
                        self.assertTrue(gate.locked())
                        thread.start()
                        self.assertTrue(following_started.wait(1))
                        self.assertFalse(
                            following_finished.wait(0.3),
                            "Next job must wait for the previous reader",
                        )
                        following_post.assert_not_called()
                        release_headers.set()
                        self.assertTrue(following_finished.wait(2))
                        self.assertEqual(errors, [])
                        self.assertEqual(answers, ["Next answer"])
                        previous_post.assert_called_once()
                        following_post.assert_called_once()
                        self.assertFalse(
                            previous_body_read.is_set(),
                            "A cancelled response must not start another read",
                        )
                    finally:
                        release_headers.set()
                        if thread.ident is not None:
                            thread.join(timeout=2)
                        acquired = gate.acquire(timeout=2)
                        if acquired:
                            gate.release()
                        self.assertTrue(
                            acquired, "Readers must release the serialization gate"
                        )

    def test_generation_waiting_for_cleanup_remains_cancellable(self):
        gate = threading.Lock()
        gate.acquire()
        waiting = threading.Event()
        cancel = threading.Event()
        finished = threading.Event()
        errors = []
        client = self.client()

        def cancel_check():
            waiting.set()
            return cancel.is_set()

        client.cancel_check = cancel_check

        def run():
            try:
                client.complete("Analyze", "Source", 100)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        with (
            patch.object(model_client, "_stream_gate", gate),
            patch.object(client, "tokenize", return_value=10),
            patch.object(client, "model", return_value="fake"),
            patch.object(client.session, "post") as post,
        ):
            try:
                thread.start()
                self.assertTrue(waiting.wait(1))
                self.assertFalse(finished.wait(0.1))
                cancel.set()
                self.assertTrue(
                    finished.wait(1), "Waiting must respond to cancellation"
                )
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], Cancelled)
                self.assertTrue(
                    gate.locked(), "Cancellation must not release another reader's lock"
                )
                post.assert_not_called()
            finally:
                cancel.set()
                if gate.locked():
                    gate.release()
                thread.join(timeout=2)
            next_client = self.client()
            self.assertEqual(
                self.complete(next_client, Stream([delta("Available", "stop")])),
                "Available",
            )

    def test_cancellation_after_acquiring_gate_releases_it_without_starting_http(self):
        gate = threading.Lock()
        cancel = threading.Event()
        client = self.client()
        client.cancel_check = cancel.is_set

        class CancellingGate:
            def acquire(self, **kwargs):
                acquired = gate.acquire(**kwargs)
                cancel.set()
                return acquired

            def release(self):
                gate.release()

        with (
            patch.object(model_client, "_stream_gate", CancellingGate()),
            patch.object(client.session, "post") as post,
        ):
            with self.assertRaises(Cancelled):
                list(client.stream_lines({}))
            post.assert_not_called()
            self.assertFalse(gate.locked())

    def test_http_reader_start_failure_does_not_leave_gate_locked(self):
        gate = threading.Lock()
        client = self.client()
        with (
            patch.object(model_client, "_stream_gate", gate),
            patch.object(
                threading.Thread,
                "start",
                side_effect=RuntimeError("Cannot start test reader"),
            ),
            patch.object(client.session, "post") as post,
        ):
            with self.assertRaises(RuntimeError):
                list(client.stream_lines({}))
            post.assert_not_called()
            self.assertFalse(gate.locked())

    def test_tokenization_cache_is_bounded_and_counts_hits(self):
        client = self.client()
        with patch.object(
            client, "json_request", return_value={"tokens": [1, 2, 3]}
        ) as request:
            self.assertEqual(client.tokenize("identical"), 3)
            self.assertEqual(client.tokenize("identical"), 3)
            request.assert_called_once()
        self.assertEqual(client.tokenization_cache_hits, 1)

    def test_negative_and_nonfinite_measurements_are_ignored(self):
        metrics = {}
        collect_metrics(
            {"usage": {"prompt_tokens": -1, "completion_tokens": float("inf")}}, metrics
        )
        self.assertEqual(metrics, {})


if __name__ == "__main__":
    unittest.main()
