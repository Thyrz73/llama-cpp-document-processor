"""Archive the bytes actually opened, without traversing substituted paths."""

import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from document_processor import files
from document_processor.errors import Cancelled, WorkerStopping
from document_processor.report_text import REPORT_FILE


class ReportArchivingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "jobs" / ("a" * 32)
        self.directory.mkdir(parents=True)
        self.path = self.directory / REPORT_FILE
        self.content = "# Étude\r\nMesures — 25 °C\r\n".encode()
        self.path.write_bytes(self.content)
        self.archive = self.directory / "previous_reports"
        self.name = hashlib.sha256(self.content).hexdigest() + ".md"
        self.external = self.root / "external"
        self.external.mkdir()
        (self.external / REPORT_FILE).write_bytes(b"PRIVATE EXTERNAL CONTENT")
        self.cancel = Mock()

    def save(self):
        files.archive_report(self.directory, self.cancel)

    def test_missing_source_is_optional_and_valid_raw_bytes_are_archived_once(self):
        self.path.unlink()
        self.save()
        self.assertFalse(self.archive.exists())
        self.path.write_bytes(self.content)
        self.save()
        archived = self.archive / self.name
        self.assertEqual(archived.read_bytes(), self.content)
        self.assertEqual(archived.stat().st_mode & 0o777, 0o600)
        before = archived.stat()
        unrelated = self.archive / "historical.md"
        unrelated.write_bytes(b"HISTORICAL")
        self.save()
        self.assertEqual(archived.stat().st_ino, before.st_ino)
        self.assertEqual(archived.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(unrelated.read_bytes(), b"HISTORICAL")
        self.assertEqual(set(self.archive.iterdir()), {archived, unrelated})
        self.path.write_bytes(b"")
        self.save()
        self.assertEqual(
            (self.archive / (hashlib.sha256(b"").hexdigest() + ".md")).read_bytes(), b""
        )

    def test_large_source_uses_one_bounded_pass_and_one_digest(self):
        content = self.content * 100000
        self.path.write_bytes(content)
        regular_file = files._regular_file
        lengths, limits = [], []

        @contextmanager
        def observe(parent, name, **kwargs):
            with regular_file(parent, name, **kwargs) as stream:

                def read(limit):
                    result = stream.read(limit)
                    limits.append(limit)
                    lengths.append(len(result))
                    return result

                yield SimpleNamespace(read=read, fileno=stream.fileno)

        with (
            patch.object(files, "_regular_file", side_effect=observe),
            patch.object(files.hashlib, "sha256", wraps=hashlib.sha256) as hashes,
        ):
            self.save()
        self.assertEqual(hashes.call_count, 1)
        self.assertEqual(sum(lengths), len(content))
        self.assertTrue(all(0 < limit <= 1024 * 1024 for limit in limits))
        self.assertEqual(
            (self.archive / (hashlib.sha256(content).hexdigest() + ".md")).read_bytes(),
            content,
        )

    def test_source_symlink_hardlink_and_special_files_are_rejected(self):
        self.path.unlink()
        external = self.external / REPORT_FILE
        for create in (
            lambda: self.path.symlink_to(external),
            lambda: os.link(external, self.path),
            lambda: os.mkfifo(self.path),
            self.path.mkdir,
        ):
            create()
            try:
                with self.assertRaises((OSError, ValueError)):
                    self.save()
                self.assertFalse(self.archive.exists())
                self.assertEqual(external.read_bytes(), b"PRIVATE EXTERNAL CONTENT")
            finally:
                if self.path.is_dir():
                    self.path.rmdir()
                else:
                    self.path.unlink()

    def test_source_substitution_before_open_is_rejected(self):
        regular_file = files._regular_file

        @contextmanager
        def substitute(parent, name, **kwargs):
            self.path.unlink()
            self.path.symlink_to(self.external / REPORT_FILE)
            with regular_file(parent, name, **kwargs) as stream:
                yield stream

        with (
            patch.object(files, "_regular_file", side_effect=substitute),
            self.assertRaises(OSError),
        ):
            self.save()
        self.assertFalse(self.archive.exists())

    def test_source_substitution_after_open_archives_only_the_opened_bytes(self):
        regular_file = files._regular_file

        @contextmanager
        def substitute(parent, name, **kwargs):
            with regular_file(parent, name, **kwargs) as stream:
                self.path.unlink()
                self.path.symlink_to(self.external / REPORT_FILE)
                yield stream

        with patch.object(files, "_regular_file", side_effect=substitute):
            self.save()
        self.assertEqual((self.archive / self.name).read_bytes(), self.content)
        self.assertEqual(
            (self.external / REPORT_FILE).read_bytes(), b"PRIVATE EXTERNAL CONTENT"
        )

    def test_mutating_or_truncating_source_during_copy_aborts_and_cleans(self):
        regular_file = files._regular_file
        for truncate in (True, False):
            with self.subTest(truncate=truncate):
                self.path.write_bytes(self.content)
                os.utime(self.path, ns=(1_000_000_000, 1_000_000_000))

                @contextmanager
                def mutate(parent, name, truncate=truncate, **kwargs):
                    with regular_file(parent, name, **kwargs) as stream:

                        def read(limit):
                            self.path.write_bytes(
                                b"" if truncate else b"x" * len(self.content)
                            )
                            return stream.read(limit)

                        yield SimpleNamespace(read=read, fileno=stream.fileno)

                with (
                    patch.object(files, "_regular_file", side_effect=mutate),
                    self.assertRaisesRegex(ValueError, "changed during archiving"),
                ):
                    self.save()
                self.assertEqual(list(self.archive.iterdir()), [])

    def test_archive_directory_symlink_and_substitution_before_open_are_rejected(self):
        self.archive.symlink_to(self.external, target_is_directory=True)
        with self.assertRaises(OSError):
            self.save()
        self.archive.unlink()
        directory = files._directory
        retained = self.directory / "retained"

        @contextmanager
        def substitute(path, parent=None):
            if path == "previous_reports":
                self.archive.rename(retained)
                self.archive.symlink_to(self.external, target_is_directory=True)
            with directory(path, parent) as descriptor:
                yield descriptor

        with (
            patch.object(files, "_directory", side_effect=substitute),
            self.assertRaises(OSError),
        ):
            self.save()
        self.assertEqual(list(retained.iterdir()), [])
        self.assertEqual(list(self.external.iterdir()), [self.external / REPORT_FILE])

    def test_destination_substitution_after_open_cannot_redirect_publication_or_cleanup(
        self,
    ):
        directory = files._directory
        retained = self.directory / "retained"

        @contextmanager
        def substitute(path, parent=None):
            with directory(path, parent) as descriptor:
                if path == "previous_reports":
                    self.archive.rename(retained)
                    self.archive.symlink_to(self.external, target_is_directory=True)
                yield descriptor

        with patch.object(files, "_directory", side_effect=substitute):
            self.save()
        self.assertEqual((retained / self.name).read_bytes(), self.content)
        self.assertEqual(list(retained.iterdir()), [retained / self.name])
        self.assertEqual(list(self.external.iterdir()), [self.external / REPORT_FILE])

    def test_existing_corrupt_or_symlinked_archive_is_never_overwritten(self):
        self.archive.mkdir()
        destination = self.archive / self.name
        destination.write_bytes(b"CORRUPTED")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.save()
        self.assertEqual(destination.read_bytes(), b"CORRUPTED")
        destination.unlink()
        destination.symlink_to(self.external / REPORT_FILE)
        with self.assertRaises(OSError):
            self.save()
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            (self.external / REPORT_FILE).read_bytes(), b"PRIVATE EXTERNAL CONTENT"
        )
        self.assertEqual(list(self.archive.iterdir()), [destination])

    def test_temporary_collision_never_opens_or_removes_foreign_entry(self):
        self.archive.mkdir()
        collision = self.archive / ".archive-controlled.tmp"
        collision.symlink_to(self.external / REPORT_FILE)
        with (
            patch.object(files.secrets, "token_hex", return_value="controlled"),
            self.assertRaises(FileExistsError),
        ):
            self.save()
        self.assertTrue(collision.is_symlink())
        self.assertEqual(
            (self.external / REPORT_FILE).read_bytes(), b"PRIVATE EXTERNAL CONTENT"
        )

    def test_cancellation_write_and_publication_failure_clean_only_owned_temporaries(
        self,
    ):
        for error, operation in (
            (Cancelled(), "_copy_digest"),
            (WorkerStopping(), "_copy_digest"),
            (OSError("Write failed"), "_copy_digest"),
            (OSError("Publication failed"), "link"),
        ):
            with self.subTest(error=type(error).__name__, operation=operation):
                owner = files.os if operation == "link" else files

                def partial_write(source, target, check_cancelled, error=error):
                    target.write(b"PARTIAL")
                    raise error

                with (
                    patch.object(
                        owner,
                        operation,
                        side_effect=error if operation == "link" else partial_write,
                    ),
                    self.assertRaises(type(error)),
                ):
                    self.save()
                self.assertEqual(list(self.archive.iterdir()), [])
                self.assertEqual(self.path.read_bytes(), self.content)

    def test_failed_temporary_open_closes_all_owned_descriptors(self):
        opened = []
        original_open, original_fdopen = os.open, os.fdopen

        def observe(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def fail_target(descriptor, mode, **kwargs):
            if mode == "wb":
                raise OSError("Cannot open temporary stream")
            return original_fdopen(descriptor, mode, **kwargs)

        with (
            patch.object(files.os, "open", side_effect=observe),
            patch.object(files.os, "fdopen", side_effect=fail_target),
            self.assertRaises(OSError),
        ):
            self.save()
        self.assertEqual(len(opened), 5)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_crash_after_atomic_publication_can_be_retried_without_rewriting_archive(
        self,
    ):
        self.archive.mkdir()
        retained = self.archive / ".archive-retained.tmp"
        retained.write_bytes(self.content)
        destination = self.archive / self.name
        os.link(retained, destination)
        before = destination.stat()
        self.save()
        self.assertEqual(destination.stat().st_ino, before.st_ino)
        self.assertEqual(destination.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(destination.read_bytes(), self.content)
        self.assertEqual(set(self.archive.iterdir()), {retained, destination})


if __name__ == "__main__":
    unittest.main()
