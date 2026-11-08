"""Cross-revision lookup stays opt-in, bounded, exact and read-only."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from document_processor import artifacts, config, database
from document_processor.artifacts import Artifacts
from document_processor.errors import ProcessingError
from document_processor.files import atomic_json
from document_processor.jobs import MAX_ARTIFACT_REVISIONS, JobStore


class ArtifactRevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.job_id = "a" * 32
        self.source = b"Original PDF identity"
        self.original = {
            "source_hash": hashlib.sha256(self.source).hexdigest(),
            "analysis_tokens": 256,
            "merge_tokens": 512,
            "final_tokens": 1024,
            "chunk_tokens": 2048,
            "context_tokens": 8192,
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "templates": {"section": "Saved instruction", "merge": "Saved merge"},
            "system_prompt": "Saved system prompt",
            "extraction": {"enabled": False},
            "pipeline_version": 2,
            "prompt_version": 1,
            "profile_version": 1,
            "schema_version": 1,
            "profile": "legacy",
            "concurrency": 1,
            "max_attempts": 8,
            "max_calls": 100,
            "step_timeout_seconds": 120,
            "request_timeout": 30,
            "report_language": "fr",
            "detail_level": "standard",
            "thinking_section": "inherit",
            "warnings": ["historical_warning"],
            "customized": False,
            "extension": {"keep": [None, 1]},
        }
        self.identity = {
            "model_id_hash": config.fingerprint("model"),
            "template_hash": config.fingerprint("template"),
            "engine_sampling_hash": config.fingerprint({"temperature": 0.2}),
            "version": "engine-v1",
        }
        self.dependencies = {
            "notes": config.fingerprint(["note", "group"]),
            "input": config.fingerprint("exact prepared input"),
            "instruction": config.fingerprint("instruction"),
            "identity": self.identity,
            "text_transport_version": 1,
            "reliability_version": 1,
        }
        self.name = "merge_00_0000.md"
        self.text = "Completed notes — é\n"
        self.store.create_job(self.job_id, "example.pdf", "Analyze", 1, self.original)
        self.directory = self.store.job_dir(self.job_id)
        self.directory.mkdir(parents=True)
        (self.directory / "source.pdf").write_bytes(self.source)
        self.previous = Artifacts(self.directory, self.original)
        self.previous.bind_identity(self.identity)
        self.previous.write(self.name, self.text, self.dependencies)
        self.current, self.revision = self.target(merge_tokens=1024)

    def target(self, **changes):
        snapshot = {**copy.deepcopy(self.original), **changes}
        revision = self.store.save_config_revision(self.job_id, snapshot)
        current = Artifacts(self.directory, snapshot)
        current.bind_identity(self.identity)
        return current, revision

    def read(self, current=None, revision=None, **options):
        return (current or self.current).read_compatible(
            options.pop("name", self.name),
            options.pop("dependencies", self.dependencies),
            store=self.store,
            job_id=self.job_id,
            revision=self.revision if revision is None else revision,
            **options,
        )

    def saved_files(self):
        return {
            str(path.relative_to(self.directory)): path.read_bytes()
            for path in self.directory.rglob("*")
            if path.is_file()
        }

    def test_compatible_budgets_and_known_metadata_keep_full_fingerprints(self):
        for changes in (
            {"analysis_tokens": 512},
            {"merge_tokens": 1024},
            {"final_tokens": 2048},
            {"warnings": ["new_warning"], "customized": True},
            {"analysis_tokens": 512, "merge_tokens": 1024, "final_tokens": 2048},
        ):
            with self.subTest(changes=changes):
                current, revision = self.target(**changes)
                result = self.read(current, revision)
                self.assertEqual(result.text, self.text)
                self.assertEqual(result.source_revision, 0)
                self.assertEqual(result.source_config_hash, self.previous.config_hash)
                self.assertNotEqual(current.config_hash, self.previous.config_hash)
                self.assertEqual(
                    current.config_hash,
                    config.fingerprint(
                        self.store.load_config(self.job_id, revision=revision)
                    ),
                )

    def test_lookup_preserves_every_original_and_target_byte_and_returns_provenance(
        self,
    ):
        before = self.saved_files()
        result = self.read()
        self.assertEqual(result.name, self.name)
        self.assertTrue(result.reused)
        self.assertEqual(result.sha256, hashlib.sha256(self.text.encode()).hexdigest())
        self.assertEqual(
            result.dependencies_hash, config.fingerprint(self.dependencies)
        )
        with self.assertRaises(FrozenInstanceError):
            result.text = "silently modified"
        self.assertEqual(self.saved_files(), before)
        self.assertFalse((self.current.root / self.name).exists())
        # A new target generation has its own normal path, never a shared link.
        self.current.write(self.name, "New result", self.dependencies)
        self.assertEqual(self.previous.read(self.name, self.dependencies), self.text)
        self.assertEqual(self.current.read(self.name, self.dependencies), "New result")

    def test_exact_dependencies_including_grouping_and_prepared_input_are_required(
        self,
    ):
        for field in self.dependencies:
            with self.subTest(field=field):
                changed = {**self.dependencies, field: "changed"}
                self.assertIsNone(self.read(dependencies=changed))
        self.assertIsNone(self.read(dependencies={}))
        self.assertIsNone(self.read(dependencies={**self.dependencies, "new": 1}))
        self.assertIsNotNone(
            self.read(dependencies=dict(reversed(list(self.dependencies.items()))))
        )

    def test_final_budget_change_does_not_bypass_final_input_matching(self):
        name = "final.md"
        self.previous.write(name, self.text, self.dependencies)
        current, revision = self.target(final_tokens=2048)
        self.assertIsNotNone(self.read(current, revision, name=name))
        for field in ("notes", "input", "instruction"):
            self.assertIsNone(
                self.read(
                    current,
                    revision,
                    name=name,
                    dependencies={**self.dependencies, field: "new preparation"},
                )
            )

    def test_unrelated_configuration_changes_are_never_ignored(self):
        for field in (
            self.original.keys() - config.RESUME_BUDGETS - config.ARTIFACT_METADATA
        ):
            with self.subTest(field=field):
                current, revision = self.target(**{field: "changed"})
                self.assertIsNone(self.read(current, revision))
        current, revision = self.target(unknown_option="added")
        self.assertIsNone(self.read(current, revision))
        without = copy.deepcopy(self.original)
        del without["extension"]
        revision = self.store.save_config_revision(self.job_id, without)
        current = Artifacts(self.directory, without)
        current.bind_identity(self.identity)
        self.assertIsNone(self.read(current, revision))

    def test_invalid_decreased_or_unverifiable_output_budgets_are_rejected(self):
        for field in config.RESUME_BUDGETS:
            for value in (None, True, 1.0, "1024", 63, 65537, self.original[field] - 1):
                with self.subTest(field=field, value=value):
                    current, revision = self.target(**{field: value})
                    self.assertIsNone(self.read(current, revision))
            missing = copy.deepcopy(self.original)
            del missing[field]
            self.assertFalse(config.compatible_artifact_config(missing, self.original))

    def test_source_file_mismatch_keeps_source_changed_error(self):
        (self.directory / "source.pdf").write_bytes(b"Changed source")
        with self.assertRaises(ProcessingError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "source_changed")

    def test_original_source_snapshot_is_required_even_if_later_revisions_agree(self):
        changed = {**self.original, "source_hash": config.fingerprint("other source")}
        old_revision = self.store.save_config_revision(self.job_id, changed)
        old = Artifacts(self.directory, changed)
        old.bind_identity(self.identity)
        old.write(self.name, self.text, self.dependencies)
        current, revision = self.target(**{**changed, "merge_tokens": 2048})
        self.assertGreater(revision, old_revision)
        self.assertIsNone(self.read(current, revision))

    def test_unknown_original_source_cannot_be_verified(self):
        for value in (None, "", "unverifiable"):
            with self.subTest(value=value):
                changed = {**self.original, "source_hash": value}
                self.assertFalse(
                    config.compatible_artifact_config(changed, self.original)
                )
                with self.store.connect() as con:
                    con.execute(
                        "UPDATE job_configs SET snapshot=?, fingerprint=? WHERE job_id=?",
                        (json.dumps(changed), config.fingerprint(changed), self.job_id),
                    )
                self.assertIsNone(self.read())

    def test_backend_model_template_sampling_version_and_missing_identity_refuse_reuse(
        self,
    ):
        for field in self.identity:
            for value in (None, "changed"):
                with self.subTest(field=field, value=value):
                    manifest = copy.deepcopy(self.previous.manifest)
                    manifest["identity"][field] = value
                    atomic_json(self.previous.path, manifest)
                    before = self.previous.path.read_bytes()
                    self.assertIsNone(self.read())
                    self.assertEqual(self.previous.path.read_bytes(), before)
        for identity in (
            None,
            {},
            [],
            {"model_id_hash": self.identity["model_id_hash"]},
        ):
            manifest = {**self.previous.manifest, "identity": identity}
            atomic_json(self.previous.path, manifest)
            self.assertIsNone(self.read())

    def test_fresh_binding_required_and_existing_identity_changed_protection_remains(
        self,
    ):
        current = Artifacts(
            self.directory, self.store.load_config(self.job_id, revision=1)
        )
        self.assertIsNone(self.read(current))
        before = self.previous.path.read_bytes()
        with self.assertRaises(ProcessingError) as caught:
            self.previous.bind_identity({**self.identity, "template_hash": "changed"})
        self.assertEqual(caught.exception.code, "identity_changed")
        self.assertEqual(self.previous.path.read_bytes(), before)
        for value in (None, "unknown", ""):
            with patch.object(
                self.current,
                "_bound_identity",
                {**self.identity, "engine_sampling_hash": value},
            ):
                self.assertIsNone(self.read())
        self.current.manifest["identity"] = {**self.identity, "version": "changed"}
        self.assertIsNone(self.read())

    def test_unknown_fresh_identity_and_unsupported_safe_open_are_misses(self):
        for index, value in enumerate((None, "unknown", "")):
            snapshot = {**self.original, "final_tokens": 4096 + index}
            revision = self.store.save_config_revision(self.job_id, snapshot)
            fresh = Artifacts(self.directory, snapshot)
            fresh.bind_identity({**self.identity, "engine_sampling_hash": value})
            self.assertIsNone(self.read(fresh, revision))
        with patch.object(os, "supports_dir_fd", set()):
            self.assertIsNone(self.read())

    def test_semantic_values_are_compared_with_their_exact_json_types(self):
        changed = {**self.original, "concurrency": True}
        self.assertFalse(config.compatible_artifact_config(self.original, changed))

    def test_corrupt_missing_invalid_utf8_and_unpublished_content_is_a_miss(self):
        path = self.previous.root / self.name
        for content in (b"corrupted", b"", b"\xff"):
            with self.subTest(content=content):
                path.write_bytes(content)
                if content == b"\xff":
                    manifest = copy.deepcopy(self.previous.manifest)
                    manifest["artifacts"][self.name]["sha256"] = hashlib.sha256(
                        content
                    ).hexdigest()
                    atomic_json(self.previous.path, manifest)
                self.assertIsNone(self.read())
        path.unlink()
        self.assertIsNone(self.read())
        path.write_text(self.text)
        manifest = copy.deepcopy(self.previous.manifest)
        del manifest["artifacts"][self.name]
        atomic_json(self.previous.path, manifest)
        self.assertIsNone(self.read())

    def test_incomplete_truncated_cancelled_or_unknown_completion_metadata_is_a_miss(
        self,
    ):
        for changes in (
            {"complete": False},
            {"complete": 1},
            {"complete": None},
            {"status": "cancelled"},
            {"truncated": True},
            {"finish_reason": "length"},
            {"stop_category": "interrupted"},
            {"sha256": "invalid"},
            {"dependencies": "invalid"},
        ):
            with self.subTest(changes=changes):
                manifest = copy.deepcopy(self.previous.manifest)
                manifest["artifacts"][self.name].update(changes)
                atomic_json(self.previous.path, manifest)
                self.assertIsNone(self.read())

    def test_malformed_incompatible_or_missing_manifest_is_a_miss(self):
        for manifest in (
            None,
            [],
            {},
            {**self.previous.manifest, "version": 2},
            {**self.previous.manifest, "version": True},
            {**self.previous.manifest, "config_hash": "other"},
            {**self.previous.manifest, "artifacts": []},
            {**self.previous.manifest, "artifacts": {self.name: []}},
        ):
            with self.subTest(manifest=manifest):
                atomic_json(self.previous.path, manifest)
                self.assertIsNone(self.read())
        for raw in (
            b"{",
            b"\xff",
            b'{"version":1,"version":1}',
            b"[" * 2000 + b"]" * 2000,
        ):
            self.previous.path.write_bytes(raw)
            self.assertIsNone(self.read())
        self.previous.path.unlink()
        self.assertIsNone(self.read())

    def test_newest_verified_exact_match_wins_and_bad_revisions_can_fall_back(self):
        first, revision1 = self.target(analysis_tokens=512)
        first.write(self.name, "Revision one", self.dependencies)
        second, revision2 = self.target(analysis_tokens=1024)
        second.write(self.name, "Revision two", self.dependencies)
        current, revision3 = self.target(analysis_tokens=2048)
        self.assertEqual(self.read(current, revision3).source_revision, revision2)
        (second.root / self.name).write_text("corrupted")
        result = self.read(current, revision3)
        self.assertEqual(
            (result.source_revision, result.text), (revision1, "Revision one")
        )
        first.write(self.name, "Different input", {"input": "different"})
        self.assertEqual(self.read(current, revision3).source_revision, 0)

    def test_future_revisions_other_jobs_and_unrecorded_directories_are_not_candidates(
        self,
    ):
        # Revision 1 itself and later revisions cannot supply an original's lookup.
        self.current.write(self.name, "Future revision", self.dependencies)
        self.assertIsNone(self.read(self.previous, 0))
        foreign = "b" * 32
        self.store.create_job(foreign, "other.pdf", "Other", 1, self.original)
        other = Artifacts(self.store.job_dir(foreign), self.original)
        other.bind_identity(self.identity)
        other.write(self.name, "Other job", self.dependencies)
        unrecorded = {**self.original, "analysis_tokens": 300}
        orphan = Artifacts(self.directory, unrecorded)
        orphan.bind_identity(self.identity)
        orphan.write(self.name, "Unrecorded", self.dependencies)
        (self.previous.root / self.name).unlink()
        self.assertIsNone(self.read())
        with self.assertRaises(ValueError):
            other.read_compatible(
                self.name,
                self.dependencies,
                store=self.store,
                job_id=self.job_id,
                revision=1,
            )

    def test_lookup_bound_is_applied_before_decoding_or_opening_candidates(self):
        for budget in range(513, 513 + MAX_ARTIFACT_REVISIONS + 2):
            self.store.save_config_revision(
                self.job_id, {**self.original, "merge_tokens": budget}
            )
        current, revision = self.target(merge_tokens=4096)
        with (
            patch.object(
                self.store, "_decode_config", wraps=self.store._decode_config
            ) as decode,
            patch.object(
                Artifacts, "_read_previous", wraps=Artifacts._read_previous
            ) as read,
        ):
            self.assertIsNone(self.read(current, revision, max_revisions=2))
            self.assertEqual(
                decode.call_count, 4
            )  # Original + current + two candidates.
            self.assertEqual(read.call_count, 0)  # Missing directories are not created.
        history = self.store.load_artifact_history(self.job_id, revision=revision)
        self.assertEqual(len(history["previous"]), MAX_ARTIFACT_REVISIONS)
        self.assertEqual(history["previous"][0]["revision"], revision - 1)
        self.assertNotIn(0, [item["revision"] for item in history["previous"]])
        self.assertIsNone(self.read(current, revision))  # No unbounded fallback to 0.
        self.assertEqual(self.read(max_revisions=1).source_revision, 0)

    def test_invalid_limits_revisions_or_wrong_target_fingerprint_are_rejected(self):
        for limit in (0, -1, True, 1.0, MAX_ARTIFACT_REVISIONS + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.read(max_revisions=limit)
        for revision in (-1, True, 1.0):
            with self.assertRaises(ValueError):
                self.read(revision=revision)
        with self.assertRaises(LookupError):
            self.read(revision=9999)
        with self.assertRaises(ProcessingError) as caught:
            self.read(revision=0)
        self.assertEqual(caught.exception.code, "artifacts_incompatible")

    def test_corrupt_persisted_snapshot_raises_config_integrity(self):
        for table in ("job_configs", "job_config_revisions"):
            with self.subTest(table=table), self.store.connect() as con:
                con.execute(f"UPDATE {table} SET fingerprint='corrupted'")
            with self.assertRaises(ProcessingError) as caught:
                self.read()
            self.assertEqual(caught.exception.code, "config_integrity")
            with self.store.connect() as con:
                if table == "job_configs":
                    con.execute(
                        "UPDATE job_configs SET fingerprint=?",
                        (config.fingerprint(self.original),),
                    )

    def test_history_rejects_non_object_and_missing_original_snapshots(self):
        with self.store.connect() as con:
            con.execute(
                "UPDATE job_configs SET snapshot='[]', fingerprint=?",
                (config.fingerprint([]),),
            )
        with self.assertRaises(ProcessingError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "config_integrity")
        with self.assertRaises(LookupError):
            self.store.load_artifact_history("b" * 32, revision=0)

    def test_history_is_one_consistent_transaction_and_uses_indexed_range(self):
        lookup = self.store._config_row
        changed = {**self.original, "merge_tokens": 2048}

        def change_after_original(con, job_id, revision):
            row = lookup(con, job_id, revision)
            if revision == 0:
                with database.connect(self.store.db_path) as writer:
                    writer.execute(
                        "UPDATE job_config_revisions SET snapshot=?, fingerprint=? "
                        "WHERE job_id=? AND revision=1",
                        (json.dumps(changed), config.fingerprint(changed), self.job_id),
                    )
            return row

        with patch.object(self.store, "_config_row", side_effect=change_after_original):
            history = self.store.load_artifact_history(self.job_id, revision=1)
        self.assertEqual(history["current"]["fingerprint"], self.current.config_hash)
        self.assertEqual(self.store.load_config(self.job_id, revision=1), changed)
        with self.store.connect() as con:
            plan = con.execute(
                "EXPLAIN QUERY PLAN SELECT revision, snapshot, fingerprint "
                "FROM job_config_revisions WHERE job_id=? AND revision<? "
                "ORDER BY revision DESC LIMIT ?",
                (self.job_id, 50, 2),
            ).fetchall()
        detail = " ".join(row["detail"] for row in plan)
        self.assertIn("SEARCH", detail)
        self.assertNotIn("SCAN", detail)
        self.assertNotIn("TEMP B-TREE", detail)

    def test_files_changed_during_reads_never_return_verified_results(self):
        state = artifacts._file_state
        # Each file is stat'ed before and after reading: source, manifest, text.
        for change_at in (2, 4, 6):
            calls = 0

            def observe(stream, change_at=change_at):
                nonlocal calls
                calls += 1
                values = state(stream)
                return (*values[:-1], values[-1] + (calls == change_at))

            with (
                self.subTest(change_at=change_at),
                patch.object(artifacts, "_file_state", side_effect=observe),
            ):
                self.assertIsNone(self.read())

    def test_content_growth_and_source_truncation_during_reads_are_misses(self):
        actual_state = artifacts._file_state
        calls = 0

        def underreported_size(stream):
            nonlocal calls
            calls += 1
            values = actual_state(stream)
            return (*values[:2], 1, *values[3:]) if calls == 3 else values

        with (
            patch.object(artifacts, "_file_state", side_effect=underreported_size),
            patch.object(artifacts, "MAX_REUSE_MANIFEST_BYTES", 2),
        ):
            self.assertIsNone(self.read())

        def truncate_source(stream):
            values = actual_state(stream)
            (self.directory / "source.pdf").write_bytes(b"")
            return values

        with patch.object(artifacts, "_file_state", side_effect=truncate_source):
            self.assertIsNone(self.read())

    def test_path_traversal_and_reserved_filenames_are_rejected(self):
        for name in (
            "../outside",
            "/tmp/outside",
            "nested/file",
            "nested\\file",
            ".",
            "..",
            "manifest.json",
            "result.md.tmp",
            "x\x00.md",
            "x" * 201,
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.read(name=name)

    def test_symlink_and_hardlinked_files_are_not_reused(self):
        for name in (self.name, "manifest.json", "source.pdf"):
            path = (
                self.directory / name
                if name == "source.pdf"
                else self.previous.root / name
            )
            content = path.read_bytes()
            external = self.store.data_dir / "external"
            external.write_bytes(content)
            for link in ("symbolic", "hard"):
                with self.subTest(name=name, link=link):
                    path.unlink()
                    if link == "symbolic":
                        path.symlink_to(external)
                    else:
                        os.link(external, path)
                    self.assertIsNone(self.read())
                    self.assertEqual(external.read_bytes(), content)
                    path.unlink()
                    path.write_bytes(content)
            external.unlink()

    def test_symlink_directories_are_not_followed(self):
        for path in (self.previous.root, self.directory / "artifacts"):
            moved = path.with_name(path.name + "-moved")
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
            self.assertIsNone(self.read())
            path.unlink()
            moved.rename(path)
        moved = self.directory.with_name("moved")
        self.directory.rename(moved)
        self.directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(LookupError):
            self.read()

    def test_special_files_and_size_limits_are_cache_misses(self):
        path = self.previous.root / self.name
        path.unlink()
        path.mkdir()
        self.assertIsNone(self.read())
        path.rmdir()
        os.mkfifo(path)
        self.assertIsNone(self.read())  # O_NONBLOCK prevents a hung lookup.
        path.unlink()
        self.previous.write(self.name, self.text, self.dependencies)
        with patch.object(artifacts, "MAX_REUSE_ARTIFACT_BYTES", 2):
            self.assertIsNone(self.read())
        with patch.object(artifacts, "MAX_REUSE_MANIFEST_BYTES", 2):
            self.assertIsNone(self.read())

    def test_same_revision_retry_behavior_and_version_one_manifest_stay_unchanged(self):
        before = self.previous.path.read_bytes()
        retry = Artifacts(self.directory, self.original)
        self.assertEqual(retry.read(self.name, self.dependencies), self.text)
        self.assertIsNone(retry.read(self.name, {"changed": True}))
        self.assertEqual(retry.path.read_bytes(), before)
        retry.write(self.name, "Retry result", self.dependencies)
        self.assertEqual(retry.read(self.name, self.dependencies), "Retry result")
        manifest = json.loads(retry.path.read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(
            set(manifest["artifacts"][self.name]),
            {"complete", "dependencies", "sha256"},
        )
        self.assertEqual(self.read().text, "Retry result")


if __name__ == "__main__":
    unittest.main()
