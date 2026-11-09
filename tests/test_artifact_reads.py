"""Read only the artifact bytes that were verified, even across file substitutions."""

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from document_processor import artifacts
from document_processor.artifacts import Artifacts
from document_processor.errors import ProcessingError
from document_processor.files import file_hash


class ArtifactReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "job"
        self.config = {"pipeline_version": 2}
        self.cache = Artifacts(self.directory, self.config)
        self.name = "analysis_0000.md"
        self.dependencies = {"input": "original"}
        self.text = "Verified notes — é\n"
        self.cache.write(self.name, self.text, self.dependencies)
        self.path = self.cache.root / self.name

    def test_valid_utf8_is_read_once_and_preserves_exact_line_endings(self):
        text = "Verified notes — é\r\nSecond line\r\n"
        self.cache.write(self.name, text, self.dependencies)
        original = artifacts._regular_file
        reads = []

        @contextmanager
        def observe(parent, name):
            with original(parent, name) as stream:
                read = Mock(wraps=stream.read)
                reads.append(read)
                yield SimpleNamespace(read=read, fileno=stream.fileno)

        before = self.path.read_bytes(), self.cache.path.read_bytes()
        with patch.object(artifacts, "_regular_file", side_effect=observe):
            self.assertEqual(self.cache.read(self.name, self.dependencies), text)
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].call_count, 1)
        self.assertEqual((self.path.read_bytes(), self.cache.path.read_bytes()), before)

    def test_replacement_after_digest_returns_only_the_verified_bytes(self):
        replacement = self.directory / "replacement.md"
        replacement.write_text("UNVERIFIED REPLACEMENT", encoding="utf-8")
        sha256 = hashlib.sha256
        swapped = False

        def swap():
            nonlocal swapped
            os.replace(replacement, self.path)
            swapped = True

        def hash_bytes(content):
            digest = sha256(content)
            swap()
            return digest

        def hash_path(path):
            digest = file_hash(path)
            swap()
            return digest

        # The legacy hook swaps after file_hash(), immediately before read_text().
        # The byte hook swaps at the same digest boundary in the corrected reader.
        with (
            patch.object(artifacts, "file_hash", side_effect=hash_path),
            patch.object(artifacts, "hashlib", SimpleNamespace(sha256=hash_bytes)),
        ):
            result = self.cache.read(self.name, self.dependencies)
        self.assertTrue(swapped, "The substitution must actually execute")
        self.assertEqual(result, self.text)
        self.assertEqual(self.path.read_text(), "UNVERIFIED REPLACEMENT")

    def test_changed_content_missing_files_and_invalid_entries_are_cache_misses(self):
        self.path.write_text("CORRUPTED", encoding="utf-8")
        self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.path.unlink()
        self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.cache.write(self.name, self.text, self.dependencies)
        entry = self.cache.manifest["artifacts"][self.name]
        for changes in (
            {"complete": False},
            {"complete": 1},
            {"dependencies": "other"},
            {"sha256": "invalid"},
        ):
            with self.subTest(changes=changes), patch.dict(entry, changes):
                self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.assertIsNone(self.cache.read("missing.md", self.dependencies))
        self.assertIsNone(self.cache.read(self.name, {"input": "different"}))

    def test_disappearance_immediately_before_open_is_a_cache_miss(self):
        original = artifacts._regular_file
        deleted = False

        @contextmanager
        def disappear(parent, name):
            nonlocal deleted
            if name == self.name:
                self.path.unlink()
                deleted = True
            with original(parent, name) as stream:
                yield stream

        with patch.object(artifacts, "_regular_file", side_effect=disappear):
            self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.assertTrue(deleted)

    def test_same_size_concurrent_mutation_during_read_is_a_cache_miss(self):
        original = artifacts._regular_file
        mutated = False

        @contextmanager
        def mutate(parent, name):
            with original(parent, name) as stream:

                def read(size):
                    nonlocal mutated
                    content = stream.read(size)
                    before = self.path.stat()
                    self.path.write_bytes(b"X" * len(content))
                    # Force an observable change even on coarse timestamp filesystems.
                    os.utime(
                        self.path,
                        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                    )
                    mutated = True
                    return content

                yield SimpleNamespace(read=read, fileno=stream.fileno)

        with patch.object(artifacts, "_regular_file", side_effect=mutate):
            self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.assertTrue(mutated)

    def test_symlink_substitution_before_open_is_rejected(self):
        external = self.directory / "external.md"
        external.write_bytes(self.path.read_bytes())
        original = artifacts._regular_file
        substituted = False

        @contextmanager
        def substitute(parent, name):
            nonlocal substituted
            if name == self.name:
                self.path.unlink()
                self.path.symlink_to(external)
                substituted = True
            with original(parent, name) as stream:
                yield stream

        with patch.object(artifacts, "_regular_file", side_effect=substitute):
            self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.assertTrue(substituted)
        self.assertEqual(external.read_text(), self.text)

    def test_linked_or_special_artifact_files_are_rejected(self):
        external = self.directory / "external.md"
        external.write_bytes(self.path.read_bytes())
        self.path.unlink()
        for link in ("symbolic", "hard"):
            with self.subTest(link=link):
                if link == "symbolic":
                    self.path.symlink_to(external)
                else:
                    os.link(external, self.path)
                self.assertIsNone(self.cache.read(self.name, self.dependencies))
                self.path.unlink()
        self.path.mkdir()
        self.assertIsNone(self.cache.read(self.name, self.dependencies))
        self.path.rmdir()
        os.mkfifo(self.path)
        self.assertIsNone(self.cache.read(self.name, self.dependencies))

    def test_symlinked_directories_cannot_redirect_reads_or_manifest_loading(self):
        for path in (
            self.cache.root,
            self.directory / "artifacts",
            self.directory,
        ):
            with self.subTest(path=path.name):
                moved = path.with_name(path.name + "-moved")
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=True)
                try:
                    self.assertIsNone(self.cache.read(self.name, self.dependencies))
                    with self.assertRaises(OSError):
                        Artifacts(self.directory, self.config)
                finally:
                    path.unlink()
                    moved.rename(path)

    def test_linked_manifest_is_refused_without_modifying_its_target(self):
        external = self.directory / "external.json"
        content = self.cache.path.read_bytes()
        external.write_bytes(content)
        for link in ("symbolic", "hard"):
            with self.subTest(link=link):
                self.cache.path.unlink()
                if link == "symbolic":
                    self.cache.path.symlink_to(external)
                else:
                    os.link(external, self.cache.path)
                with self.assertRaises((OSError, ValueError)):
                    Artifacts(self.directory, self.config)
                self.assertEqual(external.read_bytes(), content)
                self.cache.path.unlink()
                self.cache.path.write_bytes(content)

    def test_invalid_names_cannot_escape_the_artifact_directory(self):
        for name in (
            "../outside",
            str(self.path),
            "nested/file",
            "nested\\file",
            "manifest.json",
            "result.md.tmp",
            ".",
            "..",
            "x\x00.md",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.cache.read(name, self.dependencies)

    def test_matching_hash_does_not_suppress_utf8_errors(self):
        content = b"\xff"
        self.path.write_bytes(content)
        self.cache.manifest["artifacts"][self.name]["sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        with self.assertRaises(UnicodeDecodeError):
            self.cache.read(self.name, self.dependencies)

    def test_configuration_and_manifest_version_checks_remain_effective(self):
        for changes in ({"config_hash": "other"}, {"version": 2}):
            with self.subTest(changes=changes):
                self.cache.path.write_text(
                    json.dumps({**self.cache.manifest, **changes}), encoding="utf-8"
                )
                with self.assertRaises(ProcessingError) as caught:
                    Artifacts(self.directory, self.config)
                self.assertEqual(caught.exception.code, "artifacts_incompatible")


if __name__ == "__main__":
    unittest.main()
