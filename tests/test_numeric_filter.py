"""Golden numeric semantics and deterministic entity-matching work counts."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processor import checks, evidence

# Recorded before optimization from develop at 9f06dad; do not regenerate in tests.
GOLDEN = Path(__file__).parent / "fixtures" / "numeric_filter.json"


def semantic_cases():
    source = evidence.build_evidence(
        [
            {"page": 1, "text": "A · École : 12,4 → 10,8 MWh"},
            {"page": 2, "text": "A · École : 10,8 → 9,8 MWh"},
            {
                "page": 3,
                "text": "School : 10 → 12 MWh\nRate : 10 → 12 %\nZero : 0 → 2 MWh",
            },
        ],
        3,
    )
    cases = {}
    for name, suffix in (
        ("single", "[Page 1]"),
        ("multiple_ambiguous", "[Page 1] [Page 2]"),
        ("multiple_with_unknown", "[Page 1] [Page 999]"),
        ("repeated", "[Page 1] [Page 1]"),
        ("unknown", "[Page 999]"),
        ("zero", "[Page 0]"),
        ("unicode_digit", "[Page ١]"),
        ("uncited", ""),
        ("malformed_number", "[Page x]"),
        ("malformed_negative", "[Page -1]"),
        ("malformed_list", "[Pages 1, 2]"),
        ("malformed_range", "[Pages 1–2]"),
        ("lowercase", "[page 1]"),
        ("malformed_and_valid", "[Page x] [Page 1]"),
    ):
        cases[name] = (f"École (A) : variation −2,6 MWh {suffix}", source, [1, 2, 3])
    for name, answer, pages in (
        ("uncited_unique_scope", "École (A) : variation −2,6 MWh", [1]),
        ("citation_outside_scope", "École (A) : variation −2,6 MWh [Page 1]", [2]),
        ("empty_scope", "École (A) : variation −2,6 MWh [Page 1]", []),
        ("short_alias", "A: variation −2,6 MWh [Page 1]", [1, 2]),
        ("unicode_label", "Ａ · École : évolution −2,6 MWh [Page 1]", [1, 2]),
        (
            "unicode_normalization",
            "A · E\u0301cole : évolution −2,6 MWh [Page 1]",
            [1, 2],
        ),
        ("correct_value", "École (A) : variation −1,6 MWh [Page 1]", [1, 2]),
        (
            "mixed_languages",
            "École (A) : variation −2,6 MWh [Page 1]\nSchool: change +3 MWh [Page 3]",
            [1, 2, 3],
        ),
        (
            "issue_order",
            "School: change +3 MWh [Page 3]\nÉcole (A) : variation −2,6 MWh [Page 1]",
            [1, 2, 3],
        ),
        ("quoted_number", "École (A) : « variation −2,6 MWh » [Page 1]", [1, 2]),
        ("two_absolute_values", "School: change +3 MWh, quoted 12 MWh [Page 3]", [3]),
        ("unlabelled_level", "École (A) : −2,6 MWh [Page 1]", [1, 2]),
        ("absolute_and_relative", "École (A) : −12,9 % (−2,6 MWh) [Page 1]", [1, 2]),
        ("unknown_entity", "Library: change +3 MWh [Page 3]", [3]),
        ("wrong_period", "School 2024: change +3 MWh [Page 3]", [3]),
        ("percentage_unit", "Rate: change +3 % [Page 3]", [3]),
        ("zero_baseline", "Zero: change +10 % [Page 3]", [3]),
        ("no_quantities", "No numeric claim. [Page 1]", [1, 2, 3]),
    ):
        cases[name] = (answer, source, pages)
    duplicate = copy.deepcopy(source)
    duplicate["comparisons"].append(copy.deepcopy(duplicate["comparisons"][0]))
    cases["duplicate_source_rows"] = (
        "École (A) : variation −2,6 MWh [Page 1]",
        duplicate,
        [1, 2, 3],
    )
    return cases


def large_fixture():
    """120 physical pages, ten unique rows per page, 100 cited output lines."""
    source = evidence.build_evidence(
        [
            {
                "page": page,
                "text": "\n".join(
                    f"Entity {(page - 1) * 10 + row:05d}: 10 → 12 MWh"
                    for row in range(10)
                ),
            }
            for page in range(1, 121)
        ],
        120,
    )
    answer = "\n".join(
        f"Entity {(page - 1) * 10:05d}: change +3 MWh [Page {page}]"
        for page in range(1, 101)
    )
    return answer, source, set(range(1, 121))


class NumericFilterTests(unittest.TestCase):
    def test_exact_preoptimization_results_with_and_without_prepared_evidence(self):
        expected = json.loads(GOLDEN.read_text())
        cases = semantic_cases()
        self.assertEqual(set(cases), set(expected))
        for name, (answer, source, pages) in cases.items():
            for prepared in (None, checks.prepare_evidence(source)):
                with self.subTest(case=name, prepared=prepared is not None):
                    result = checks.numeric_checks(
                        answer, source, pages, prepared=prepared
                    )
                    # Serialization also freezes key order, issue order and Unicode text.
                    self.assertEqual(
                        json.dumps(result, ensure_ascii=False),
                        json.dumps(expected[name], ensure_ascii=False),
                    )

    def test_large_cited_fixture_matches_only_rows_on_each_cited_page(self):
        answer, source, pages = large_fixture()
        self.assertEqual(len(source["comparisons"]), 1200)
        prepared = checks.prepare_evidence(source)
        with patch.object(
            checks, "_entity_in_line", wraps=checks._entity_in_line
        ) as match:
            result = checks.numeric_checks(answer, source, pages, prepared=prepared)
        self.assertEqual(match.call_count, 1000)
        self.assertEqual(result["checked"], 100)
        self.assertEqual(result["unchecked_lines_or_values"], 0)
        self.assertEqual(
            [item["page"] for item in result["issues"]], list(range(1, 101))
        )
        self.assertTrue(
            all(
                item["expected"] == "2" and item["actual"] == "3"
                for item in result["issues"]
            )
        )

    def test_uncited_lines_keep_all_input_page_candidates_and_cached_normalization(
        self,
    ):
        source = semantic_cases()["single"][1]
        prepared = checks.prepare_evidence(source)
        with (
            patch.object(
                checks, "_entity_in_line", wraps=checks._entity_in_line
            ) as match,
            patch.object(checks, "_normalized", wraps=checks._normalized) as normalize,
        ):
            result = checks.numeric_checks(
                "École (A): variation −2,6 MWh", source, {1, 2}, prepared=prepared
            )
        self.assertEqual(match.call_count, 2)
        self.assertEqual(normalize.call_count, 1)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["unchecked_lines_or_values"], 1)
        self.assertEqual(result["issues"], [])

    def test_cited_page_filter_also_respects_input_scope(self):
        source = semantic_cases()["single"][1]
        with patch.object(
            checks, "_entity_in_line", wraps=checks._entity_in_line
        ) as match:
            result = checks.numeric_checks(
                "École (A): variation −2,6 MWh [Page 1]", source, {2}
            )
        match.assert_not_called()
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["unchecked_lines_or_values"], 1)


if __name__ == "__main__":
    unittest.main()
