"""Check maintenance and benchmark tools without running a model or service."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from document_processor import files
from document_processor.jobs import JobStore
from scripts import benchmark, check_jobs


class ToolTests(unittest.TestCase):
    def test_benchmark_default_is_offline_and_rejects_combined_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "variants.json"
            plan.write_text(json.dumps([{"name": "baseline", "options": {"profile": "low"}},
                                        {"name": "smaller", "options": {"profile": "low", "overrides": {"chunk_tokens": 2500}}}]))
            with patch("requests.Session", side_effect=AssertionError("Dry runs must be offline")), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(benchmark.main(["--variants", str(plan)]), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["network_requests"], 0)
            self.assertEqual(result["trials"], ["baseline", "smaller", "smaller", "baseline", "baseline", "smaller"])
        with self.assertRaises(ValueError):
            benchmark.validate_plan([{"name": "base", "options": {"profile": "low"}},
                                     {"name": "high", "options": {"profile": "high"}}])

    def test_mechanical_quality_checks_are_explicit_and_do_not_export_terms(self):
        result = benchmark.recall_checks("Temperature 25 °C [Page 1]", [
            {"id": "temperature", "terms": ["25", "°C", "[Page 1]"]},
            {"id": "missing_fact", "terms": ["PRIVATE_EXPECTED_FACT"]}])
        self.assertEqual([item["matched"] for item in result], [True, False])
        self.assertNotIn("PRIVATE_EXPECTED_FACT", json.dumps(result))

    def test_read_only_job_inspector_preserves_files_and_flags_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "store", JobStore(root)), \
                 patch.object(app, "read_system_prompt", return_value="PRIVATE"):
                app.store.initialize()
                response = app.app.test_client().post("/api/jobs", data={
                    "file": (io.BytesIO(b"%PDF-test"), "example.pdf")})
                identifier = response.json["id"]
                # The inspector is meant for a stopped processor: a running one keeps its log and
                # shared-memory files open, and every reader updates the latter.
                app.store.close()
                before = {path: files.file_hash(path) for path in root.rglob("*") if path.is_file()}
                result = check_jobs.inspect(root)
                self.assertEqual((result["snapshots_valid"], result["integrity_errors"]), (1, 0))
                for path, digest in before.items():
                    self.assertEqual(files.file_hash(path), digest)
                (app.store.job_dir(identifier) / "source.pdf").write_bytes(b"changed")
                self.assertEqual(check_jobs.inspect(root)["integrity_errors"], 1)


if __name__ == "__main__":
    unittest.main()
