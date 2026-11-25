"""Compact metadata changes representation only, retaining atomic publication."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processor import files, pipeline, reliability
from document_processor.artifacts import Artifacts


class CompactMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "metadata.json"

    def assert_compact(self, path, expected):
        encoded = path.read_bytes()
        self.assertEqual(json.loads(encoded), expected)
        self.assertEqual(
            encoded,
            json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )

    def test_default_json_representation_is_unchanged_and_compact_is_canonical(self):
        data = {
            "z": "École 中文 → −2,6",
            "a": [None, True, False, 42, 1.5, {"x": "\n"}],
        }
        files.atomic_json(self.path, data)
        self.assertEqual(
            self.path.read_bytes(),
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        files.atomic_json(self.path, data, compact=True)
        self.assert_compact(self.path, data)
        saved = self.path.read_bytes()
        files.atomic_json(self.path, dict(reversed(list(data.items()))), compact=True)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertIn("École 中文".encode(), saved)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_nonfinite_values_are_rejected_before_touching_existing_file(self):
        files.atomic_json(self.path, {"previous": True})
        saved = self.path.read_bytes()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                files.atomic_json(self.path, {"nested": [value]}, compact=True)
            self.assertEqual(self.path.read_bytes(), saved)
            self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_partial_temp_write_and_failed_replacement_leave_published_json_intact(
        self,
    ):
        previous = {"previous": "Résumé"}
        updated = {"updated": "École 中文"}
        files.atomic_json(self.path, previous)
        saved = self.path.read_bytes()
        write_text = Path.write_text

        def interrupted_write(path, text, **kwargs):
            write_text(path, text[:5], **kwargs)
            raise OSError("simulated interrupted temporary write")

        for target, replacement in (
            ("pathlib.Path.write_text", interrupted_write),
            ("document_processor.files.os.replace", OSError("interrupted replacement")),
        ):
            with (
                self.subTest(target=target),
                patch(target, autospec=True, side_effect=replacement),
                self.assertLogs("document-processor", level="ERROR"),
                self.assertRaises(OSError),
            ):
                files.atomic_json(self.path, updated, compact=True)
            self.assertEqual(self.path.read_bytes(), saved)
        files.atomic_json(self.path, updated, compact=True)
        self.assert_compact(self.path, updated)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_pretty_manifest_remains_readable_and_new_entries_follow_artifact_publication(
        self,
    ):
        artifacts = Artifacts(self.root, {"pipeline_version": 2})
        identity = {
            "model_id_hash": "model",
            "template_hash": "template",
            "engine_sampling_hash": "sampling",
        }
        dependencies = {"input": "École"}
        artifacts.bind_identity(identity)
        artifacts.write("first.md", "Texte 中文", dependencies)
        files.atomic_json(artifacts.path, artifacts.manifest)
        old = artifacts.path.read_bytes()
        self.assertIn(b'\n  "version": 1', old)
        reopened = Artifacts(self.root, {"pipeline_version": 2})
        self.assertEqual(reopened.read("first.md", dependencies), "Texte 中文")
        self.assertEqual(reopened.path.read_bytes(), old)
        reopened.bind_identity(identity)
        self.assert_compact(reopened.path, reopened.manifest)
        previous = reopened.path.read_bytes()
        replace = os.replace
        publications = []

        def observe(tmp, destination):
            publications.append(destination.name)
            self.assertEqual(reopened.path.read_bytes(), previous)
            if destination == reopened.path:
                entry = json.loads(tmp.read_bytes())["artifacts"]["second.md"]
                self.assertTrue(entry["complete"])
                self.assertEqual(
                    entry["sha256"],
                    hashlib.sha256(
                        (reopened.root / "second.md").read_bytes()
                    ).hexdigest(),
                )
            replace(tmp, destination)

        with patch.object(files.os, "replace", side_effect=observe):
            reopened.write("second.md", "−1,6 MWh", dependencies)
        self.assertEqual(publications, ["second.md", "manifest.json"])
        self.assert_compact(reopened.path, reopened.manifest)
        recovered = Artifacts(self.root, {"pipeline_version": 2})
        self.assertEqual(recovered.read("first.md", dependencies), "Texte 中文")
        self.assertEqual(recovered.read("second.md", dependencies), "−1,6 MWh")
        self.assertIsNone(recovered.read("second.md", {"input": "different"}))

    def test_manifest_interruption_rejects_unpublished_or_overwritten_artifacts_and_recovers(
        self,
    ):
        for name in ("new.md", "saved.md"):
            with self.subTest(name=name):
                artifacts = Artifacts(self.root / name, {"pipeline_version": 2})
                artifacts.write("saved.md", "old text", {})
                published = artifacts.path.read_bytes()
                replace = os.replace

                def interrupt(
                    tmp, destination, manifest=artifacts.path, publish=replace
                ):
                    if destination == manifest:
                        raise OSError("manifest publication interrupted")
                    publish(tmp, destination)

                with (
                    patch.object(files.os, "replace", side_effect=interrupt),
                    self.assertLogs("document-processor", level="ERROR"),
                    self.assertRaises(OSError),
                ):
                    artifacts.write(name, "new text 中文", {})
                self.assertEqual(artifacts.path.read_bytes(), published)
                self.assertEqual((artifacts.root / name).read_text(), "new text 中文")
                recovered = Artifacts(self.root / name, {"pipeline_version": 2})
                self.assertIsNone(recovered.read(name, {}))
                if name != "saved.md":
                    self.assertEqual(recovered.read("saved.md", {}), "old text")
                recovered.write(name, "new text 中文", {})
                recovered.write("later.md", "later text", {})
                reopened = Artifacts(self.root / name, {"pipeline_version": 2})
                self.assertEqual(reopened.read(name, {}), "new text 中文")
                self.assertEqual(reopened.read("later.md", {}), "later text")
                self.assert_compact(reopened.path, reopened.manifest)

    def test_quality_ledger_rewrites_retain_all_steps_exact_checks_and_warning_order(
        self,
    ):
        run = pipeline.JobRun.__new__(pipeline.JobRun)
        run.artifacts = Artifacts(self.root, {"pipeline_version": 2})
        run.quality = {"version": reliability.VERSION, "steps": {}}
        run.evidence = reliability.build_evidence(
            [{"page": 1, "text": "École : 12,4 → 10,8 MWh"}], 1
        )
        run._generation_audit = None
        run._prepared_evidence = None
        answer = "École : −2,6 MWh [Page 1]. [Page 99]"
        expected = reliability.audit(answer, run.evidence, [1])
        path = run.artifacts.root / "quality_checks.json"
        for index, name in enumerate(("section.md", "rapport_final.md", "section.md")):
            check = run._check(name, answer, [1, 1], [index], 100)
            self.assertEqual(
                check,
                {
                    **expected,
                    "input_pages": [1],
                    "source_sections": [index],
                    "evidence_prompt_limit": 100,
                },
            )
            self.assert_compact(path, run.quality)
        self.assertEqual(
            set(json.loads(path.read_bytes())["steps"]),
            {"section.md", "rapport_final.md"},
        )
        self.assertEqual(run.quality["steps"]["section.md"]["source_sections"], [2])
        for language in ("fr", "en"):
            self.assertEqual(
                reliability.report_warnings(json.loads(path.read_bytes()), language),
                reliability.report_warnings(run.quality, language),
            )
        saved = path.read_bytes()
        with (
            patch.object(
                files.os, "replace", side_effect=OSError("interrupted ledger")
            ),
            self.assertLogs("document-processor", level="ERROR"),
            self.assertRaises(OSError),
        ):
            run._check("later.md", answer, [1], [3])
        self.assertEqual(path.read_bytes(), saved)
        run._check("later.md", answer, [1], [3])
        self.assert_compact(path, run.quality)
        self.assertEqual(len(json.loads(path.read_bytes())["steps"]), 3)


if __name__ == "__main__":
    unittest.main()
