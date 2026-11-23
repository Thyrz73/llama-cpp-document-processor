"""Read-only integrity checks across original and resumed configurations."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from document_processor.artifacts import MAX_REUSE_MANIFEST_BYTES, Artifacts
from document_processor.config import fingerprint
from document_processor.files import atomic_json, file_hash
from document_processor.jobs import JobStore
from scripts import check_jobs


class InspectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = JobStore(self.root)
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.identifier = "b" * 32
        self.directory = self.store.job_dir(self.identifier)
        self.directory.mkdir(parents=True)
        (self.directory / "source.pdf").write_bytes(b"%PDF-PRIVATE_PDF")
        self.original = {
            "source_hash": file_hash(self.directory / "source.pdf"),
            "analysis_tokens": 256,
            "system_prompt": "PRIVATE_PROMPT",
            "max_attempts": 5,
        }
        self.store.create_job(
            self.identifier, "private.pdf", "PRIVATE_QUESTION", 1, self.original
        )
        self.configs = [self.original]
        for budget in (512, 1024):
            config = {**self.original, "analysis_tokens": budget}
            self.store.save_config_revision(self.identifier, config)
            self.configs.append(config)
        self.artifacts = []
        for config in self.configs:
            item = Artifacts(self.directory, config)
            item.write("analysis_0000.md", "PRIVATE_TEXT", {"input": "PRIVATE_INPUT"})
            self.artifacts.append(item)
        self.store.update_job(self.identifier, status="failed")
        self.assertEqual(
            self.store.requeue(self.identifier, 5, config_revision=2)[0], "queued"
        )
        self.assertEqual(self.store.claim_next_job(), self.identifier)
        recorder = self.store.start_attempt(
            self.identifier, self.store.get_job(self.identifier)["queued_at"]
        )
        self.run_id = recorder.run_id
        recorder.finish("failed")
        self.store.close()

    def inspect(self):
        self.store.close()
        return check_jobs.inspect(self.root)

    def corrupt_sql(self, sql, parameters=()):
        # Deliberately bypass foreign keys to simulate a damaged backup.
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as con, con:
            con.execute(sql, parameters)

    def write_manifest(self, value):
        atomic_json(self.artifacts[2].path, value)

    def test_all_original_selected_and_unselected_work_is_counted_without_mutations(
        self,
    ):
        before = {
            p.relative_to(self.root): (file_hash(p), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file()
        }
        result = self.inspect()
        self.assertEqual(
            result,
            {
                "jobs": 1,
                "historical_without_snapshot": 0,
                "snapshots_valid": 3,
                "artifacts_valid": 3,
                "integrity_errors": 0,
            },
        )
        after = {
            p.relative_to(self.root): (file_hash(p), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)

    def test_selected_and_unselected_snapshot_corruption(self):
        for revision in (1, 2):
            for raw in ("{broken", "{}", "[]", '{"duplicate":1,"duplicate":2}'):
                with self.subTest(revision=revision, raw=raw):
                    self.corrupt_sql(
                        "UPDATE job_config_revisions SET snapshot=? WHERE revision=?",
                        (raw, revision),
                    )
                    self.assertGreater(self.inspect()["integrity_errors"], 0)
                    self.corrupt_sql(
                        "UPDATE job_config_revisions SET snapshot=? WHERE revision=?",
                        (json.dumps(self.configs[revision]), revision),
                    )

    def test_revision_fingerprint_and_source_mismatch(self):
        self.corrupt_sql(
            "UPDATE job_config_revisions SET fingerprint=? WHERE revision=2",
            ("0" * 64,),
        )
        self.assertGreater(self.inspect()["integrity_errors"], 0)
        changed = {**self.configs[2], "source_hash": "0" * 64}
        self.corrupt_sql(
            "UPDATE job_config_revisions SET snapshot=?,fingerprint=? WHERE revision=2",
            (json.dumps(changed), fingerprint(changed)),
        )
        self.assertGreater(self.inspect()["integrity_errors"], 0)

    def test_modified_revision_artifact_is_reported(self):
        (self.artifacts[2].root / "analysis_0000.md").write_text("CORRUPTED")
        self.assertEqual(self.inspect()["artifacts_valid"], 2)
        self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_missing_manifest_in_existing_directory_is_an_error(self):
        self.artifacts[2].path.unlink()
        self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_malformed_manifests_and_entries(self):
        good = self.artifacts[2].manifest
        variants = [
            [],
            {},
            {**good, "version": True},
            {**good, "config_hash": "0" * 64},
            {**good, "artifacts": []},
        ]
        for entry in (
            None,
            {},
            {"complete": False},
            {"complete": True, "dependencies": {}, "sha256": "0" * 64},
        ):
            variants.append({**good, "artifacts": {"analysis_0000.md": entry}})
        for value in variants:
            with self.subTest(value=value):
                self.write_manifest(value)
                self.assertGreater(self.inspect()["integrity_errors"], 0)
        for raw in ("{", '{"version":1,"version":1}', "[" * 1100):
            self.artifacts[2].path.write_text(raw)
            self.assertGreater(self.inspect()["integrity_errors"], 0)

    def test_oversized_manifest_is_rejected(self):
        self.artifacts[2].path.write_bytes(b" " * (MAX_REUSE_MANIFEST_BYTES + 1))
        self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_unsafe_entry_names_are_rejected(self):
        for name in ("../source.pdf", "/etc/passwd", "manifest.json", "file.tmp"):
            value = copy.deepcopy(self.artifacts[2].manifest)
            value["artifacts"] = {name: value["artifacts"]["analysis_0000.md"]}
            self.write_manifest(value)
            self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_artifact_symlinks_hardlinks_and_special_files_are_rejected(self):
        path = self.artifacts[2].root / "analysis_0000.md"
        path.unlink()
        path.symlink_to(self.artifacts[0].root / path.name)
        self.assertEqual(self.inspect()["integrity_errors"], 1)
        path.unlink()
        os.link(self.artifacts[0].root / path.name, path)
        self.assertGreater(self.inspect()["integrity_errors"], 0)
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_linked_manifest_and_directory_are_rejected(self):
        path = self.artifacts[2].path
        path.unlink()
        path.symlink_to(self.artifacts[0].path)
        self.assertEqual(self.inspect()["integrity_errors"], 1)
        path.unlink()
        self.artifacts[2].root.rename(self.directory / "moved")
        self.artifacts[2].root.symlink_to(
            self.directory / "moved", target_is_directory=True
        )
        self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_unsafe_source_and_job_identity_are_rejected(self):
        source = self.directory / "source.pdf"
        source.rename(self.directory / "real.pdf")
        source.symlink_to(self.directory / "real.pdf")
        self.assertEqual(self.inspect()["integrity_errors"], 1)
        source.unlink()
        (self.directory / "real.pdf").rename(source)
        self.corrupt_sql("UPDATE jobs SET id='../escape'")
        self.assertGreater(self.inspect()["integrity_errors"], 0)

    def test_broken_selection_and_attempt_references(self):
        cases = [
            (
                "UPDATE job_config_selections SET revision=999",
                "UPDATE job_config_selections SET revision=2",
            ),
            (
                "UPDATE job_run_config_revisions SET revision=999",
                "UPDATE job_run_config_revisions SET revision=2",
            ),
            (
                "UPDATE job_run_config_revisions SET job_id='missing'",
                "UPDATE job_run_config_revisions SET job_id='" + self.identifier + "'",
            ),
            (
                "UPDATE job_run_config_revisions SET run_id=999",
                "UPDATE job_run_config_revisions SET run_id=" + str(self.run_id),
            ),
        ]
        for damage, restore in cases:
            with self.subTest(damage=damage):
                self.corrupt_sql(damage)
                self.assertGreater(self.inspect()["integrity_errors"], 0)
                self.corrupt_sql(restore)
        self.assertEqual(self.inspect()["integrity_errors"], 0)

    def test_absent_artifact_work_is_allowed(self):
        import shutil

        shutil.rmtree(self.directory / "artifacts")
        self.assertEqual(self.inspect()["integrity_errors"], 0)
        (self.directory / "artifacts").mkdir()
        self.assertEqual(self.inspect()["integrity_errors"], 0)

    def test_pre_revision_schema_and_jobs_without_snapshots(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with closing(sqlite3.connect(root / "jobs.sqlite3")) as con, con:
                con.execute("CREATE TABLE jobs(id TEXT)")
                con.execute("INSERT INTO jobs VALUES (?)", (self.identifier,))
            self.assertEqual(check_jobs.inspect(root)["historical_without_snapshot"], 1)
            with closing(sqlite3.connect(root / "jobs.sqlite3")) as con, con:
                con.execute(
                    "CREATE TABLE job_configs(job_id TEXT,snapshot TEXT,fingerprint TEXT)"
                )
                con.execute(
                    "INSERT INTO job_configs VALUES (?,?,?)",
                    (
                        self.identifier,
                        json.dumps(self.original),
                        fingerprint(self.original),
                    ),
                )
            job = root / "jobs" / self.identifier
            job.mkdir(parents=True)
            (job / "source.pdf").write_bytes(b"%PDF-PRIVATE_PDF")
            self.assertEqual(check_jobs.inspect(root)["integrity_errors"], 0)
            self.assertEqual(check_jobs.inspect(root)["snapshots_valid"], 1)

    def test_uncheckpointed_wal_is_refused_without_mutations(self):
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as con:
            con.execute("UPDATE jobs SET stage='pending WAL'")
            con.commit()
            before = {
                p.name: (file_hash(p), p.stat().st_mtime_ns)
                for p in self.root.iterdir()
                if p.is_file()
            }
            with self.assertRaises(ValueError):
                self.inspect()
            after = {
                p.name: (file_hash(p), p.stat().st_mtime_ns)
                for p in self.root.iterdir()
                if p.is_file()
            }
            self.assertEqual(before, after)

    def test_missing_artifact_and_invalid_utf8_manifest_are_reported(self):
        (self.artifacts[2].root / "analysis_0000.md").unlink()
        self.assertEqual(self.inspect()["integrity_errors"], 1)
        self.artifacts[2].path.write_bytes(b"\xff")
        self.assertEqual(self.inspect()["integrity_errors"], 1)

    def test_missing_revision_table_with_references_is_reported(self):
        self.corrupt_sql("DROP TABLE job_config_revisions")
        self.assertGreater(self.inspect()["integrity_errors"], 0)

    def test_revision_checks_continue_after_a_corrupt_original(self):
        self.corrupt_sql("UPDATE job_configs SET snapshot='{}'")
        result = self.inspect()
        self.assertEqual(result["snapshots_valid"], 2)
        self.assertEqual(result["artifacts_valid"], 2)
        self.assertEqual(result["integrity_errors"], 1)

    def test_unsafe_database_is_rejected_privately_by_cli(self):
        database = self.root / "jobs.sqlite3"
        database.rename(self.root / "private.sqlite3")
        database.symlink_to(self.root / "private.sqlite3")
        result = subprocess.run(
            [sys.executable, "scripts/check_jobs.py", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr.strip(), "Cannot inspect the selected data directory."
        )

    def test_cli_status_and_private_data_absent_from_output(self):
        for expected in (0, 1):
            result = subprocess.run(
                [sys.executable, "scripts/check_jobs.py", str(self.root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(
                bool(json.loads(result.stdout)["integrity_errors"]), bool(expected)
            )
            self.assertNotIn("PRIVATE", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            (self.artifacts[2].root / "analysis_0000.md").write_text(
                "PRIVATE_CORRUPTION"
            )


if __name__ == "__main__":
    unittest.main()
