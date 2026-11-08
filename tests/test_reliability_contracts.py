"""Golden outputs recorded before splitting reliability version 1 into modules."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processor import checks, evidence, jobs, pipeline, prompts, report_text
from document_processor import reliability as r
from document_processor.artifacts import Artifacts
from document_processor.config import fingerprint

# Reuse the existing documentary fixtures rather than inventing a second corpus.
# isort: split
import test_reliability as fixtures

GOLDEN = Path(__file__).parent / "fixtures" / "reliability_v1.json"


def serialized_hash(value):
    """Include dictionary/issue order and exact Unicode text in the golden check."""
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def scenarios():
    return {
        "french": (
            [
                {"page": 1, "text": "Document fictif. Hypothèse H1 non vérifiée."},
                {"page": 2, "text": fixtures.TABLE},
                {"page": 3, "text": fixtures.RESERVE + "\n\n" + fixtures.MISSING},
            ],
            3,
            [],
            [],
            [1, 2, 3],
            "École (A) : −12,9 % (−2,6 MWh) [Page 2].\n"
            "Conclusion : l’intervention a entraîné la baisse. [Page 4]\n"
            f"« {fixtures.MISSING} » [Page 2]\n"
            "Aucun étalonnage n’a été effectué. [Page 3]",
        ),
        "english": (
            [
                {
                    "page": 1,
                    "text": "Fictional document. Assumption H1 is not verified.",
                },
                {"page": 2, "text": "School: 12.4 → 10.8 MWh"},
                {
                    "page": 3,
                    "text": "Observed after treatment, without proof of causality. "
                    "The protocol does not mention calibration.",
                },
            ],
            3,
            [],
            [],
            [1, 2, 3],
            "School: change −2.6 MWh [Page 2]\nTreatment caused the decrease.\n"
            "No calibration was performed.\n"
            '"The protocol does not mention calibration." [Page 2]\n[Pages 3-1]',
        ),
        "ocr_unicode": (
            [
                {
                    "page": 1,
                    "text": "Ａ · École : 12,4 → 10,8 MWh\n" + fixtures.MISSING,
                },
                {"page": 2, "text": ""},
                {
                    "page": 3,
                    "text": "A : 12,O → 10,8 MWh\nA : 1,234.5 → 2 MWh\n"
                    "Printed reference [Page 99]. Hypothèse H2 non vérifiée.",
                },
            ],
            4,
            [2, 4],
            [1, 2],
            [1, 2, 3],
            f"A · École : variation −2,6 MWh [Page 1]\n« {fixtures.MISSING} » [Page 3]\n"
            "[Pages 1–4] [Page 99]",
        ),
        "ambiguous_tables": (
            [
                {"page": 1, "text": fixtures.TABLE},
                {"page": 2, "text": fixtures.ARROWS},
                {"page": 3, "text": "Zero: 0 → 2 MWh\nRate: 10 → 12 %"},
            ],
            3,
            [],
            [],
            [1, 2, 3],
            "TOTAL: variation −10,0 %\nÉcole (A): −12,9 % (−2,6 MWh) [Page 1]\n"
            "Zero: change +10 % [Page 3]\nRate: change +20 % [Page 3]",
        ),
        "empty_ocr": (
            [{"page": 1, "text": ""}],
            2,
            [1, 2],
            [1, 2],
            [1, 2],
            "[Page 2] [Pages 1-999999999]",
        ),
    }


def contract(case):
    pages, count, missing, ocr, scope, answer = case
    evidence = r.build_evidence(pages, count, missing, ocr)
    notes = [
        r.note(
            "Original model notes [Page 99].", scope, [2, 0, 2], ["nonexistent_page"]
        ),
        r.note("Other notes, unchanged.", scope, [1]),
    ]
    merged = r.merge_note("Merged model text, unchanged.", notes)
    check = r.audit(answer, evidence, scope)
    quality = {
        "version": r.VERSION,
        "steps": {
            "old.md": r.audit("[Page 99]", evidence, scope),
            "rapport_final.md": check,
        },
    }
    return {
        "evidence": evidence,
        "evidence_fingerprint": fingerprint(evidence),
        "blocks": {
            str(limit): r.evidence_block(evidence, scope, limit)
            for limit in (0, 239, 240, 500, 2400)
        },
        "merged_note": merged,
        "combined": r.combined(notes, evidence),
        "compact": r.combined(notes, evidence, 0),
        "prepare_input": [
            r.prepare_input(
                notes, evidence, lambda text, limit=limit: len(text) < limit
            )
            for limit in (0, 500, 10000)
        ],
        "numeric": r.numeric_checks(answer, evidence, scope),
        "quotes": r.quote_checks(answer, evidence, scope),
        "certainty": r.certainty_checks(answer, evidence, scope),
        "audit": check,
        "quality": quality,
        "warnings": {
            language: r.report_warnings(quality, language)
            for language in ("fr", "en", "French", "français", "auto", "de")
        },
        "headers": {
            language: prompts.report_header(
                {"report_language": language},
                "École <test>\r\nrapport.pdf",
                count,
                2,
                missing,
                ocr,
            )
            for language in ("fr", "en", "French", "français", "auto", "de")
        },
        "saved_language_header": prompts.report_header(
            {"report_language": "auto", "effective_report_language": "fr"},
            "study.pdf",
            count,
            2,
            missing,
            ocr,
        ),
    }


class ReliabilityContractTests(unittest.TestCase):
    def test_facade_reexports_the_owners_without_wrappers(self):
        for owner, names in (
            (
                evidence,
                (
                    "build_evidence",
                    "evidence_block",
                    "note",
                    "merge_note",
                    "combined",
                    "prepare_input",
                ),
            ),
            (
                checks,
                (
                    "numeric_checks",
                    "quote_checks",
                    "citation_checks",
                    "certainty_checks",
                    "audit",
                    "PreparedEvidence",
                    "prepare_evidence",
                ),
            ),
            (report_text, ("MESSAGES", "report_warnings")),
        ):
            for name in names:
                with self.subTest(module=owner.__name__, name=name):
                    self.assertIs(getattr(r, name), getattr(owner, name))
        for name in ("is_french", "deterministic_language", "report_header"):
            self.assertIs(getattr(prompts, name), getattr(report_text, name))

    def test_report_names_and_translation_languages_stay_compatible(self):
        self.assertEqual(report_text.REPORT_FILE, "rapport_final.md")
        self.assertEqual(jobs.REPORT_FILE, report_text.REPORT_FILE)
        self.assertEqual(pipeline.FINAL_ARTIFACT, report_text.REPORT_FILE)
        translations = json.loads(
            (GOLDEN.parents[2] / "static" / "translations.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report_text.MESSAGES.keys(), translations.keys())
        self.assertEqual(
            report_text.MESSAGES["fr"].keys(), report_text.MESSAGES["en"].keys()
        )

    def test_localized_report_copy_never_becomes_a_model_instruction(self):
        config = {
            "templates": prompts.TEMPLATES,
            "report_language": "fr",
            "detail_level": "standard",
        }
        instruction = prompts.instruction(config, "final", "Résumez le document.")
        quality = {
            "steps": {
                report_text.REPORT_FILE: {
                    "issues": [{"code": "nonexistent_page", "reference": "[Page 99]"}]
                }
            }
        }
        with patch.dict(report_text.MESSAGES["fr"], title="Localized report only"):
            self.assertIn("Localized report only", r.report_warnings(quality, "fr"))
            self.assertEqual(
                prompts.instruction(config, "final", "Résumez le document."),
                instruction,
            )

    def test_existing_fixtures_match_pre_split_outputs_and_warning_order(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(r.VERSION, golden["version"])
        self.assertEqual(
            fingerprint(prompts.TEMPLATES), golden["templates_fingerprint"]
        )
        self.assertEqual(r.MESSAGES, golden["messages"])
        for name, case in scenarios().items():
            with self.subTest(name=name):
                actual = contract(case)
                self.assertEqual(actual.keys(), golden["cases"][name].keys())
                for field, value in actual.items():
                    with self.subTest(field=field):
                        self.assertEqual(
                            serialized_hash(value), golden["cases"][name][field]
                        )
                self.assertEqual(
                    [issue["code"] for issue in actual["audit"]["issues"]],
                    golden["issue_order"][name],
                )

    def test_pre_split_cache_manifest_remains_reusable_without_rewriting(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cache = golden["cache"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "artifacts" / cache["manifest"]["config_hash"]
            root.mkdir(parents=True)
            manifest = json.dumps(cache["manifest"], ensure_ascii=False)
            (root / "manifest.json").write_text(manifest, encoding="utf-8")
            for name, text in cache["files"].items():
                (root / name).write_text(text, encoding="utf-8")
            artifacts = Artifacts(temporary, cache["config"])
            for name, text in cache["files"].items():
                self.assertEqual(
                    artifacts.read(name, cache["dependencies"][name]), text
                )
                self.assertEqual(
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    cache["manifest"]["artifacts"][name]["sha256"],
                )
            old_evidence = json.loads(cache["files"]["source_evidence.json"])
            case = scenarios()["french"]
            self.assertEqual(
                serialized_hash(r.audit(case[-1], old_evidence, case[-2])),
                golden["cases"]["french"]["audit"],
            )
            self.assertEqual(
                (root / "manifest.json").read_text(encoding="utf-8"), manifest
            )


if __name__ == "__main__":
    unittest.main()
