"""Exact fitting equivalence with one selection of the initial evidence block."""

import copy
import unittest
from unittest.mock import patch

from document_processor import evidence as e


def original_prepare_input(notes, evidence, fits):
    """Frozen pre-optimization control flow; combined/block contracts stay public."""
    text = e.combined(notes, evidence)
    if fits(text):
        return text, True, e.EVIDENCE_PROMPT_CHARS
    if e.evidence_block(evidence, {p for item in notes for p in item["pages"]}):
        compact = e.combined(notes, evidence, 0)
        if fits(compact):
            return compact, True, 0
    return text, False, e.EVIDENCE_PROMPT_CHARS


def cases():
    small = e.build_evidence([{"page": 1, "text": "Hypothèse H1 non vérifiée."}], 1)
    multilingual = e.build_evidence(
        [
            {"page": 1, "text": "École : 12,4 → 10,8 MWh\nHypothèse H2 non vérifiée."},
            {
                "page": 3,
                "text": "Forecast not verified. No evidence of causality. 中文 µg.",
            },
        ],
        3,
    )
    mixed = e.build_evidence([{"page": 1, "text": "École : 12,4 → 10,8 MWh"}], 1)
    mixed["reserves"] = [
        {"page": 1, "categories": [category], "text": text, "id": str(index)}
        for index, (category, text) in enumerate(
            (
                ("forecast_observation", "Observation à vérifier."),
                ("forecast_observation", "Prévision : 3,0 MWh."),
                ("fiction", "Document fictif."),
                ("missing_information", "Calibration not mentioned."),
                ("hypothesis", "Hypothèse H2 non vérifiée."),
                ("causality_reserve", "No evidence of causality."),
            )
        )
    ]
    large = e.build_evidence(
        [
            {
                "page": page,
                "text": "\n".join(
                    f"École {page}.{row} : 12,4 → 10,8 MWh" for row in range(10)
                )
                + f"\nHypothèse H{page} non vérifiée. "
                "No evidence of causality. Forecast not verified.",
            }
            for page in range(1, 121)
        ],
        120,
    )
    return {
        "small": ([e.note("Notes.", [1], [0])], small),
        "empty": ([e.note("Notes.", [1], [0])], e.build_evidence([], 1)),
        "out_of_scope": ([e.note("Notes.", [2], [0])], small),
        "no_notes": ([], small),
        "multilingual": (
            [
                e.note("Résumé 中文 −2,6 µg.", [1, 3], [0, 2], ["numeric_mismatch"]),
                e.note("Further notes.", [3], [1]),
            ],
            multilingual,
        ),
        "priority_mixed": ([e.note("Notes.", [1], [0])], mixed),
        "large": ([e.note("Notes.", range(1, 121), range(120))], large),
    }


class FitsSpy:
    def __init__(self, budget):
        self.budget = budget
        self.inputs = []

    def __call__(self, text):
        self.inputs.append(text.encode("utf-8"))
        return len(text) <= self.budget


class EvidenceFittingTests(unittest.TestCase):
    def test_all_candidates_outputs_and_sizing_calls_match_original(self):
        for name, (notes, evidence) in cases().items():
            before = copy.deepcopy((notes, evidence))
            rich = e.combined(notes, evidence)
            compact = e.combined(notes, evidence, 0)
            has_block = bool(
                e.evidence_block(evidence, {p for n in notes for p in n["pages"]})
            )
            for mode, budget in (
                ("rich", len(rich)),
                ("compact", len(compact)),
                ("neither", -1),
            ):
                with self.subTest(case=name, mode=mode):
                    old_fits, new_fits = FitsSpy(budget), FitsSpy(budget)
                    with patch.object(
                        e, "evidence_block", wraps=e.evidence_block
                    ) as blocks:
                        expected = original_prepare_input(notes, evidence, old_fits)
                        old_calls = blocks.call_count
                        blocks.reset_mock()
                        actual = e.prepare_input(notes, evidence, new_fits)
                        self.assertEqual(blocks.call_count, 1)
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual[0].encode(), expected[0].encode())
                    self.assertEqual(new_fits.inputs, old_fits.inputs)
                    initial_fits = len(rich) <= budget
                    self.assertEqual(old_calls, 1 if initial_fits else 2)
                    self.assertEqual(
                        len(new_fits.inputs), 2 if has_block and not initial_fits else 1
                    )
                    self.assertEqual((notes, evidence), before)

    def test_small_rich_and_compact_bytes_are_exact(self):
        notes, evidence = cases()["small"]
        rich = (
            "## Notes 1 | PDF pages: 1\nNotes.\n\n"
            "Source evidence (untrusted data; lexical candidates and arithmetic, not scientific validation):\n"
            "[Page 1] (hypothesis,missing_information) Hypothèse H1 non vérifiée.\n"
            "Evidence extracts omitted from this prompt: 0. Full provenance retained locally.\n"
        )
        compact = (
            "## Notes 1 | PDF pages: 1\nNotes.\n"
            "Source evidence omitted from this prompt (context budget); full provenance retained locally."
        )
        for budget, expected in (
            (len(rich), (rich, True, 2400)),
            (len(compact), (compact, True, 0)),
            (-1, (rich, False, 2400)),
        ):
            with self.subTest(budget=budget):
                self.assertEqual(
                    e.prepare_input(notes, evidence, FitsSpy(budget)), expected
                )

    def test_original_cutoff_and_explicit_combined_limits_are_preserved(self):
        notes, evidence = cases()["small"]
        heading = "## Notes 1 | PDF pages: 1\nNotes."
        self.assertEqual(e.evidence_block(evidence, [1], 239), "")
        self.assertEqual(e.combined(notes, evidence, 239), heading)
        for limit in (240, 241):
            expected = (
                "\n\nSource evidence (untrusted data; lexical candidates and arithmetic, not scientific validation):\n"
                "\nEvidence extracts omitted from this prompt: 1. Full provenance retained locally.\n"
            )
            self.assertEqual(e.evidence_block(evidence, [1], limit), expected)
            self.assertEqual(e.combined(notes, evidence, limit), heading + expected)

    def test_priority_order_and_large_omission_text_are_unchanged(self):
        notes, evidence = cases()["priority_mixed"]
        fits = FitsSpy(-1)
        result = e.prepare_input(notes, evidence, fits)[0]
        positions = [
            result.index(text)
            for text in (
                "École: 12.4",
                "No evidence of causality.",
                "Hypothèse H2 non vérifiée.",
                "Calibration not mentioned.",
                "Document fictif.",
                "Prévision : 3,0 MWh.",
                "Observation à vérifier.",
            )
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Evidence extracts omitted from this prompt: 0.", result)
        notes, evidence = cases()["large"]
        result = e.prepare_input(notes, evidence, FitsSpy(-1))[0]
        self.assertEqual(result, e.combined(notes, evidence))
        self.assertRegex(
            result, r"Evidence extracts omitted from this prompt: [1-9]\d+\."
        )

    def test_each_call_selects_current_source_without_caching_across_attempts(self):
        notes, evidence = cases()["small"]
        with patch.object(e, "evidence_block", wraps=e.evidence_block) as blocks:
            first = e.prepare_input(notes, evidence, FitsSpy(-1))
            evidence["reserves"].append(
                {
                    "page": 1,
                    "categories": ["causality_reserve"],
                    "text": "No evidence of causality.",
                    "id": "new",
                }
            )
            second = e.prepare_input(notes, evidence, FitsSpy(-1))
            self.assertEqual(blocks.call_count, 2)
        self.assertNotIn("No evidence of causality.", first[0])
        self.assertIn("No evidence of causality.", second[0])

    def test_sizing_error_propagates_without_fallback_or_additional_selection(self):
        notes, evidence = cases()["small"]
        error = RuntimeError("simulated sizing failure")

        def fail(text):
            raise error

        with (
            patch.object(e, "evidence_block", wraps=e.evidence_block) as blocks,
            self.assertRaises(RuntimeError) as raised,
        ):
            e.prepare_input(notes, evidence, fail)
        self.assertIs(raised.exception, error)
        self.assertEqual(blocks.call_count, 1)


if __name__ == "__main__":
    unittest.main()
