"""Reuse only the just-generated audit, while preserving evidence and warning output."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# This module may be discovered before the existing app fixtures initialize their environment.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pdf-audit-test-"))
os.environ.setdefault(
    "SYSTEM_PROMPT_FILE",
    str(Path(__file__).resolve().parents[1] / "config/system_prompt.example.txt"),
)

from document_processor import pipeline
from document_processor import reliability as r
from document_processor.artifacts import Artifacts

# Preserve fixture initialization after the application dependencies.
# isort: split
import test_reliability as fixtures

ANSWER = (
    "École (A) : −12,9 % (−2,6 MWh) [Page 2].\n"
    "Conclusion : l’intervention a entraîné la baisse. [Page 4]\n"
    f"« {fixtures.MISSING} » [Page 2]\nAucun étalonnage n’a été effectué. [Page 3]"
)


class AuditReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.run = self.new_run()

    def new_run(self):
        run = pipeline.JobRun.__new__(pipeline.JobRun)
        run.evidence = fixtures.evidence()
        run._prepared_evidence = None
        run._generation_audit = None
        run.quality = {"version": r.VERSION, "steps": {}}
        run.artifacts = Artifacts(self.directory, {"pipeline_version": 2})
        run.model = Mock(
            tokenization_calls=0, tokenization_ms=0, tokenization_cache_hits=0
        )
        run.model.complete.return_value = ANSWER
        run.runtime = SimpleNamespace(live_output=False, preview_chars=1000)
        run.recorder = Mock()
        run.config = {"max_calls": 50}
        run.calls = 0
        return run

    def generate(self):
        return self.run._generate("section", "instructions", "source", 100, [1, 2, 3])

    def test_generated_audit_is_consumed_once_without_changing_its_warning_order(self):
        expected = r.audit(ANSWER, self.run.evidence, [1, 2, 3])
        with patch.object(r, "audit", wraps=r.audit) as audit:
            answer = self.generate()
            check = self.run._check("analysis_0000.md", answer, [3, 2, 1, 2], [0], 0)
            self.assertEqual(audit.call_count, 1)
            self.assertEqual(
                check,
                {
                    **expected,
                    "input_pages": [1, 2, 3],
                    "source_sections": [0],
                    "evidence_prompt_limit": 0,
                },
            )
            self.run._check("second.md", answer, [1, 2, 3], [1])
            self.assertEqual(audit.call_count, 2)
        self.assertEqual(
            [issue["code"] for issue in check["issues"]],
            [
                "nonexistent_page",
                "numeric_mismatch",
                "certainty_escalation_candidate",
                "missing_information_escalation_candidate",
                "quote_page_mismatch",
            ],
        )
        metrics = self.run.model.current_metrics["quality_checks"]
        self.assertEqual(metrics["warnings"], len(expected["issues"]))
        self.assertEqual(metrics["numeric_checks"], expected["numeric"]["checked"])
        self.assertEqual(metrics["source"], "mechanical_warning_only")
        self.assertFalse(metrics["semantic_validation"])
        self.assertEqual(
            json.loads((self.run.artifacts.root / "quality_checks.json").read_text()),
            self.run.quality,
        )

    def test_exact_content_pages_version_and_evidence_batch_are_required(self):
        for change in ("content", "pages", "version", "evidence"):
            with (
                self.subTest(change=change),
                patch.object(r, "audit", wraps=r.audit) as audit,
            ):
                self.run = self.new_run()
                answer = self.generate()
                pages = [1, 2, 3]
                if change == "content":
                    answer += " "  # No NFKC, whitespace or approximate content match is allowed.
                elif change == "pages":
                    pages = [2]
                elif change == "evidence":
                    self.run.evidence = copy.deepcopy(self.run.evidence)
                with patch.object(r, "VERSION", r.VERSION + (change == "version")):
                    self.run._check("output.md", answer, pages, [0])
                self.assertEqual(audit.call_count, 2)

    def test_reused_artifact_matching_pending_generation_is_audited_afresh(self):
        dependencies = {"source": "same"}
        self.run.artifacts.write(
            "cached.md",
            ANSWER,
            {
                **dependencies,
                "text_transport_version": 1,
                "reliability_version": r.VERSION,
            },
        )
        with patch.object(r, "audit", wraps=r.audit) as audit:
            self.generate()
            answer = self.run._step(
                "cached.md",
                "section",
                dependencies,
                Mock(side_effect=AssertionError("Must reuse")),
            )
            self.run._check("cached.md", answer, [1, 2, 3], [0])
            self.assertEqual(audit.call_count, 2)

    def test_other_attempts_do_not_reuse_a_generation_audit(self):
        with patch.object(r, "audit", wraps=r.audit) as audit:
            answer = self.generate()
            other = self.new_run()
            other.evidence = self.run.evidence
            other._check("other.md", answer, [1, 2, 3], [0])
            self.assertEqual(audit.call_count, 2)

    def test_normalization_is_prepared_once_per_batch_without_mutating_persistent_data(
        self,
    ):
        evidence = fixtures.evidence()
        before = json.dumps(evidence, ensure_ascii=False)
        with patch.object(r, "_normalized", wraps=r._normalized) as normalize:
            prepared = r.prepare_evidence(evidence)
            self.assertIs(r.prepare_evidence(evidence, prepared), prepared)
            first = r.audit(ANSWER, evidence, [1, 2, 3], prepared=prepared)
            second = r.audit(ANSWER, evidence, [1, 2, 3], prepared=prepared)
            calls = [call.args[0] for call in normalize.call_args_list]
        for item in evidence["comparisons"]:
            self.assertEqual(calls.count(item["entity"]), 1)
            self.assertEqual(calls.count(item["raw"]), 1)
        self.assertEqual(first, second)
        self.assertEqual(first, r.audit(ANSWER, evidence, [1, 2, 3]))
        self.assertEqual(json.dumps(evidence, ensure_ascii=False), before)
        other = copy.deepcopy(evidence)
        self.assertIsNot(r.prepare_evidence(other, prepared), prepared)
        with patch.object(r, "VERSION", r.VERSION + 1):
            self.assertIsNot(r.prepare_evidence(evidence, prepared), prepared)

    def test_prepared_nfkc_entities_and_quotes_match_existing_unicode_behavior(self):
        evidence = r.build_evidence(
            [
                {
                    "page": 1,
                    "text": "Ａ · École : 12,4 → 10,8 MWh\n" + fixtures.MISSING,
                },
                {"page": 2, "text": "Unrelated page"},
            ],
            2,
        )
        answer = (
            f"A · École : variation −2,6 MWh [Page 1]\n« {fixtures.MISSING} » [Page 2]"
        )
        prepared = r.prepare_evidence(evidence)
        self.assertEqual(prepared.entities, ("a école",))
        self.assertEqual(
            r.audit(answer, evidence, [1, 2]),
            r.audit(answer, evidence, [1, 2], prepared=prepared),
        )
        self.assertEqual(
            [issue["code"] for issue in r.audit(answer, evidence, [1, 2])["issues"]],
            ["numeric_mismatch", "quote_page_mismatch"],
        )


class PipelineAuditReuseTests(unittest.TestCase):
    def test_generation_and_retry_have_one_audit_per_output_and_identical_quality_files(
        self,
    ):
        fixture = fixtures.PipelineReliabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        identifier = fixture.create()
        with (
            patch.object(fixtures.WordModel, "final_output", ANSWER),
            patch.object(r, "audit", wraps=r.audit) as audit,
        ):
            fixture.run_job(identifier, 5)
            calls = len(fixtures.WordModel.calls)
            self.assertEqual(calls, 7)  # five sections, one merge, one final
            self.assertEqual(audit.call_count, calls)
            root, quality = fixture.load_checks(identifier)
            evidence = (root / "source_evidence.json").read_bytes()
            saved = (root / "quality_checks.json").read_bytes()
            warnings = {
                language: r.report_warnings(quality, language)
                for language in ("fr", "en")
            }
            audit.reset_mock()
            fixture.run_job(identifier, 5)
            self.assertEqual(len(fixtures.WordModel.calls), calls)
            self.assertEqual(
                audit.call_count,
                calls,
                "Reused outputs are checked again in the new attempt",
            )
            self.assertEqual((root / "source_evidence.json").read_bytes(), evidence)
            self.assertEqual((root / "quality_checks.json").read_bytes(), saved)
            _, retried = fixture.load_checks(identifier)
            for language in warnings:
                self.assertEqual(
                    r.report_warnings(retried, language), warnings[language]
                )


if __name__ == "__main__":
    unittest.main()
