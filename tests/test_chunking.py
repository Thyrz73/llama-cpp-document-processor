"""Exact chunk budgets, lossless passages and offline tokenizer instrumentation."""

import json
import random
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from document_processor import extraction, pipeline
from document_processor.artifacts import Artifacts
from document_processor.config import fingerprint
from document_processor.errors import (
    Cancelled,
    ModelError,
    ProcessingError,
    WorkerStopping,
)
from document_processor.model_client import ModelClient
from document_processor.profiles import PROFILES

# Deterministic subword-like boundaries, including whitespace/word merges and Unicode.
# This is an offline endpoint simulator, not a claim about any llama.cpp vocabulary.
SUBWORDS = re.compile(
    r"\[Page|\r\n|\n\n| [A-Za-z]{1,4}|[A-Za-z]{1,4}|\d{1,3}|[^\x00-\x7f]|.", re.S
)


def subword_count(text):
    return len(SUBWORDS.findall(text))


def fixture_pages(count):
    return [
        {
            "page": page,
            "text": "\n\n".join(
                f"Observation {page}.{paragraph}: Scientific measurements record temperature 25 °C and uncertainty ±0.5. "
                "Methods compare calibrated instruments, numerical results and independent samples. "
                "Limitations include missing observations and a small population. "
                "Résumé des résultats : hypothèse à vérifier, énergie et précision."
                for paragraph in range(1, 4)
            ),
        }
        for page in range(1, count + 1)
    ]


class SimulatedTokenizer(ModelClient):
    """Use the real client/cache/cancellation logic against a simulated /tokenize."""

    def __init__(self, counter=subword_count):
        super().__init__("http://unused.invalid", "", {}, "")
        self.counter = counter
        self.method_calls = 0
        self.method_characters = 0
        self.http_characters = 0

    def tokenize(self, text):
        self.method_calls += 1
        self.method_characters += len(text)
        return super().tokenize(text)

    def json_request(self, method, path, **kwargs):
        if (
            method != "POST"
            or path != "/tokenize"
            or kwargs["json"]["add_special"] is not False
        ):
            raise AssertionError("Unexpected tokenizer request")
        text = kwargs["json"]["content"]
        self.http_characters += len(text)
        return {"tokens": list(range(self.counter(text)))}


class ChunkingTests(unittest.TestCase):
    def assert_chunks(self, pages, model, budget):
        original = [dict(page) for page in pages]
        chunks = extraction.make_chunks(pages, model, budget)
        self.assertEqual(pages, original, "Chunking must not mutate extracted pages")
        actual = [
            (part["page"], character)
            for chunk in chunks
            for part in chunk["parts"]
            for character in part["text"]
        ]
        expected = [
            (page["page"], character) for page in pages for character in page["text"]
        ]
        self.assertEqual(
            actual,
            expected,
            "Every character and its physical page must survive exactly once, in order",
        )
        for chunk in chunks:
            self.assertTrue(chunk["parts"])
            self.assertTrue(all(part["text"] for part in chunk["parts"]))
            self.assertEqual(
                chunk["text"],
                "\n\n".join(
                    f"[Page {part['page']}]\n{part['text']}" for part in chunk["parts"]
                ),
            )
            self.assertEqual(
                chunk["pages"], sorted({part["page"] for part in chunk["parts"]})
            )
            self.assertLessEqual(model.tokenize(chunk["text"]), budget)
        return chunks

    def tokenizer(self, counter=subword_count):
        model = SimulatedTokenizer(counter)
        self.addCleanup(model.session.close)
        return model

    def test_one_three_and_120_pages_fit_low_and_ultra_profiles(self):
        for count in (1, 3, 120):
            for profile in ("low", "ultra"):
                with self.subTest(pages=count, profile=profile):
                    self.assert_chunks(
                        fixture_pages(count),
                        self.tokenizer(),
                        PROFILES[profile]["chunk_tokens"],
                    )

    def test_long_fixture_reduces_endpoint_work_with_headroom_for_regressions(self):
        for profile, calls in (("low", 160), ("ultra", 140)):
            with self.subTest(profile=profile):
                model = self.tokenizer()
                extraction.make_chunks(
                    fixture_pages(120), model, PROFILES[profile]["chunk_tokens"]
                )
                self.assertLessEqual(model.tokenization_calls, calls)
                self.assertLess(model.http_characters, 400000)

    def test_seeded_unicode_and_non_monotone_tokenization_preserve_all_characters(self):
        generator = random.Random(73)
        alphabet = (
            "word",
            " ",
            "\n",
            "\t",
            "é",
            "中",
            "👩🏽‍🔬",
            "\u00a0",
            "[Page 999]",
        )

        def non_monotone(text):
            # The interaction can grow or shrink when a passage is added.
            joins = text.count("\n\n[Page ")
            return len(text.encode("utf-8")) // 3 + joins * 17 * (len(text) % 5)

        for case in range(30):
            pages = [
                {
                    "page": page * 2 + 1,
                    "text": "".join(
                        generator.choice(alphabet)
                        for _ in range(generator.randint(1, 100))
                    ),
                }
                for page in range(generator.randint(1, 12))
            ]
            budget = generator.choice((8, 32, 128, 3000, 7000))
            with self.subTest(case=case, budget=budget):
                self.assert_chunks(pages, self.tokenizer(non_monotone), budget)

    def test_underestimated_non_additive_groups_are_repaired_before_emission(self):
        # Solo counts plus all heuristic headroom fit; every multi-passage group does not.
        def adversarial(text):
            return 500 if text.count("[Page ") > 1 else 1

        pages = [{"page": page, "text": f"Passage {page}"} for page in range(1, 33)]
        chunks = self.assert_chunks(pages, self.tokenizer(adversarial), 400)
        self.assertEqual(len(chunks), len(pages))

    def test_repair_can_keep_fitting_subgroups_and_is_not_linear_in_prefix_size(self):
        def adversarial(text):
            count = text.count("[Page ")
            return 5 if count == 1 else 100 if count <= 3 else 1000

        pages = [{"page": page, "text": f"Part {page}"} for page in range(1, 25)]
        chunks = self.assert_chunks(pages, self.tokenizer(adversarial), 300)
        self.assertTrue(any(len(chunk["parts"]) > 1 for chunk in chunks))
        self.assertTrue(all(len(chunk["parts"]) <= 3 for chunk in chunks))

    def test_limit_candidates_are_checked_and_sum_overestimates_can_still_fit(self):
        # Heuristic additions cross the budget, but a context-dependent merge fits.
        pages = [{"page": page, "text": f"Part {page}"} for page in range(1, 5)]
        chunks = self.assert_chunks(
            pages,
            self.tokenizer(lambda text: 50 if text.count("[Page ") == 1 else 60),
            100,
        )
        self.assertEqual(len(chunks), 1)

    def test_sub_50_character_passages_can_be_split_to_tiny_budgets(self):
        for budget in (10, 11, 17):
            with self.subTest(budget=budget):
                self.assert_chunks(
                    [{"page": 1, "text": "abcdefghijklmnopqrstu"}],
                    self.tokenizer(len),
                    budget,
                )

    def test_unsplittable_marker_or_single_character_retains_chunking_failed(self):
        for budget in (1, 3, 9):
            with (
                self.subTest(budget=budget),
                self.assertRaises(ProcessingError) as error,
            ):
                extraction.make_chunks(
                    [{"page": 123, "text": "a"}], self.tokenizer(len), budget
                )
            self.assertEqual(error.exception.code, "chunking_failed")

    def test_long_words_paragraphs_headings_spaces_and_unicode_are_lossless(self):
        texts = [
            "A" * 16001,
            "\t  " + "mot " * 5000 + "\n\n  ",
            "Intro\n" + "line\n" * 1200 + "## Heading\nFollowing paragraph\n",
            "\u2003\t" + "é👩🏽‍🔬e\u0301 中文\u00a0" * 900 + "\n\t ",
            " \n\t" * 3000,
        ]
        for text in texts:
            with self.subTest(length=len(text)):
                self.assert_chunks([{"page": 7, "text": text}], self.tokenizer(), 128)

    def test_embedded_page_markers_do_not_change_physical_page_metadata(self):
        pages = [
            {"page": 7, "text": "See [Page 999].  " * 500},
            {"page": 2, "text": "\nOther physical page\n"},
            {"page": 7, "text": "Back to seven"},
        ]
        chunks = self.assert_chunks(pages, self.tokenizer(), 128)
        self.assertNotIn(999, {page for chunk in chunks for page in chunk["pages"]})

    def test_empty_and_whitespace_only_inputs_are_handled_without_inventing_text(self):
        self.assertEqual(extraction.make_chunks([], self.tokenizer(), 1), [])
        self.assertEqual(
            extraction.make_chunks([{"page": 1, "text": ""}], self.tokenizer(), 1), []
        )
        self.assert_chunks([{"page": 3, "text": " \t\n\n "}], self.tokenizer(), 100)

    def test_invalid_budgets_and_strange_token_counts_fail_closed(self):
        for budget in (0, -1, True, 1.5, "10", None):
            with (
                self.subTest(budget=budget),
                self.assertRaises(ProcessingError) as error,
            ):
                extraction.make_chunks(
                    [{"page": 1, "text": "text"}], self.tokenizer(), budget
                )
            self.assertEqual(error.exception.code, "chunking_failed")
        # Keep callback signatures; these fixtures are consumed within each iteration.
        for value in (-1, True, None, 1.5, float("inf"), float("nan"), "10", [], {}):
            with self.subTest(value=value), self.assertRaises(ProcessingError) as error:
                extraction.make_chunks(
                    [{"page": 1, "text": "text"}],
                    SimpleNamespace(tokenize=lambda _: value),  # noqa: B023
                    100,
                )
            self.assertEqual(error.exception.code, "chunking_failed")
        self.assert_chunks(fixture_pages(3), self.tokenizer(lambda _: 0), 1)

    def test_remote_errors_cancellation_and_shutdown_propagate_unchanged(self):
        for signal in (
            ModelError("invalid_tokenizer", "Invalid endpoint response"),
            Cancelled(),
            WorkerStopping(),
        ):
            with self.subTest(signal=type(signal).__name__):
                tokenizer = Mock(side_effect=signal)
                with self.assertRaises(type(signal)) as error:
                    extraction.make_chunks(
                        fixture_pages(1), SimpleNamespace(tokenize=tokenizer), 100
                    )
                self.assertIs(error.exception, signal)

    def test_malformed_endpoint_responses_keep_the_client_error(self):
        for payload in (None, [], {}, {"tokens": "not a list"}, {"tokens": -1}):
            with self.subTest(payload=payload):
                model = self.tokenizer()
                with (
                    patch.object(model, "json_request", return_value=payload),
                    self.assertRaises(ModelError) as error,
                ):
                    extraction.make_chunks(fixture_pages(1), model, 3000)
                self.assertEqual(error.exception.code, "invalid_tokenizer")

    def test_errors_after_accepted_passages_do_not_return_partial_results(self):
        signal = ModelError("backend_unavailable", "Unavailable")
        tokenizer = Mock(side_effect=[20, 20, signal])
        with self.assertRaises(ModelError) as error:
            extraction.make_chunks(
                fixture_pages(3), SimpleNamespace(tokenize=tokenizer), 100
            )
        self.assertIs(error.exception, signal)

    def test_local_count_cache_hits_still_check_cancellation(self):
        model = self.tokenizer()
        model.cancel_check = Mock(side_effect=[False, True])
        with self.assertRaises(Cancelled):
            extraction.make_chunks(
                [{"page": 1, "text": "same"}, {"page": 1, "text": "same"}], model, 100
            )
        self.assertEqual(model.tokenization_calls, 1)

    def test_new_chunk_format_invalidates_only_the_affected_artifact_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Artifacts(Path(directory), {"pipeline_version": 2})
            extracted = {"pages": [{"page": 1, "text": "source"}]}
            dependencies = {
                "extracted": fingerprint(extracted),
                "budget": 3000,
                "identity": None,
            }
            old = json.dumps(
                [{"text": "old", "pages": [1], "parts": [{"page": 1, "text": "old"}]}]
            )
            artifacts.write(
                "chunks.json", old, {**dependencies, "chunk_format_version": 2}
            )
            artifacts.write("extracted.json", "unaffected", {"source": "same"})
            artifacts.write(
                "analysis_0000.md", "unaffected note", {"chunk": "unchanged"}
            )
            run = pipeline.JobRun.__new__(pipeline.JobRun)
            run.config = {"chunk_tokens": 3000}
            run.identity = None
            run.model = self.tokenizer()
            run._phase = Mock()
            run.store = Mock()
            run.job_id = "a" * 32

            def step(name, stage, deps, compute):
                cached = artifacts.read(name, deps)
                if cached is not None:
                    return cached
                answer = compute()
                artifacts.write(name, answer, deps)
                return answer

            run._step = step
            result = run._split(extracted)
            self.assertNotEqual(result, json.loads(old))
            self.assertEqual(
                artifacts.manifest["artifacts"]["chunks.json"]["dependencies"],
                fingerprint({**dependencies, "chunk_format_version": 3}),
            )
            self.assertEqual(
                artifacts.read("extracted.json", {"source": "same"}), "unaffected"
            )
            self.assertEqual(
                artifacts.read("analysis_0000.md", {"chunk": "unchanged"}),
                "unaffected note",
            )
            with patch.object(
                extraction, "make_chunks", side_effect=AssertionError("Reuse v3 chunks")
            ):
                self.assertEqual(run._split(extracted), result)


if __name__ == "__main__":
    unittest.main()
