"""Exclusive temporary writes preserve unrelated files and completed destinations."""

import itertools
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processor import files
from document_processor.artifacts import Artifacts
from document_processor.files import atomic_json, atomic_text


class AtomicFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "destination"
        self.directory.mkdir()
        self.path = self.directory / "report.md"
        self.external = self.root / "external.md"
        self.external.write_text("EXTERNAL CONTENT", encoding="utf-8")

    def test_legacy_temporary_symlink_cannot_overwrite_external_file(self):
        legacy = self.path.with_suffix(self.path.suffix + ".tmp")
        legacy.symlink_to(self.external)
        atomic_text(self.path, "Verified report — é\n")
        self.assertEqual(self.path.read_text(), "Verified report — é\n")
        self.assertEqual(self.external.read_text(), "EXTERNAL CONTENT")
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(set(self.directory.iterdir()), {self.path, legacy})

    def test_exclusive_creation_skips_a_colliding_symlink(self):
        collision = self.directory / f".{self.path.name}.occupied.tmp"
        collision.symlink_to(self.external)
        with patch.object(
            tempfile, "_get_candidate_names", return_value=iter(["occupied", "owned"])
        ):
            atomic_text(self.path, "Completed")
        self.assertEqual(self.path.read_text(), "Completed")
        self.assertEqual(self.external.read_text(), "EXTERNAL CONTENT")
        self.assertTrue(collision.is_symlink())
        self.assertEqual(set(self.directory.iterdir()), {self.path, collision})

    def test_failed_exclusive_creation_never_removes_an_unowned_file(self):
        collision = self.directory / f".{self.path.name}.occupied.tmp"
        self.path.write_text("Original", encoding="utf-8")
        for symbolic in (True, False):
            with self.subTest(symbolic=symbolic):
                if symbolic:
                    collision.symlink_to(self.external)
                else:
                    collision.write_text("Other writer's temporary", encoding="utf-8")
                with (
                    patch.object(tempfile, "TMP_MAX", 2),
                    patch.object(
                        tempfile,
                        "_get_candidate_names",
                        return_value=itertools.repeat("occupied"),
                    ),
                    patch.object(files, "event") as event,
                    self.assertRaises(FileExistsError),
                ):
                    atomic_text(self.path, "Must not replace")
                self.assertEqual(self.path.read_text(), "Original")
                self.assertEqual(self.external.read_text(), "EXTERNAL CONTENT")
                self.assertEqual(collision.is_symlink(), symbolic)
                if not symbolic:
                    self.assertEqual(collision.read_text(), "Other writer's temporary")
                event.assert_called_once()
                collision.unlink()

    def test_successful_replace_publishes_only_closed_complete_utf8_content(self):
        self.path.write_text("Original", encoding="utf-8")
        content = "Électricité — 測定\r\nSecond line\n"
        original = os.replace
        observed = []

        def replace(source, destination):
            source = Path(source)
            self.assertEqual(source.parent, self.directory)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(self.path.read_text(), "Original")
            self.assertEqual(source.read_bytes(), content.encode("utf-8"))
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
            observed.append(source)
            original(source, destination)

        with patch.object(files.os, "replace", side_effect=replace):
            atomic_text(self.path, content)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.path.read_bytes(), content.encode("utf-8"))
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_destination_symlink_is_replaced_without_changing_external_target(self):
        self.path.symlink_to(self.external)
        atomic_text(self.path, "New report")
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(self.path.read_text(), "New report")
        self.assertEqual(self.external.read_text(), "EXTERNAL CONTENT")

    def test_write_errors_and_interruptions_remove_only_the_created_temporary(self):
        self.path.write_text("Original", encoding="utf-8")
        foreign = self.directory / "foreign.tmp"
        foreign.write_text("Do not remove", encoding="utf-8")
        original = tempfile.NamedTemporaryFile
        for error in (OSError("Controlled write failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                opened = []

                def create(*args, opened=opened, error=error, **kwargs):
                    stream = original(*args, **kwargs)
                    opened.append(stream)
                    write = stream.write

                    def interrupted(text):
                        write(text[:3])
                        raise error

                    stream.write = interrupted
                    return stream

                with (
                    patch.object(
                        files.tempfile, "NamedTemporaryFile", side_effect=create
                    ),
                    patch.object(files, "event") as event,
                    self.assertRaises(type(error)) as caught,
                ):
                    atomic_text(self.path, "Incomplete replacement")
                self.assertIs(caught.exception, error)
                self.assertEqual(len(opened), 1)
                self.assertTrue(opened[0].closed)
                self.assertFalse(Path(opened[0].name).exists())
                self.assertEqual(self.path.read_text(), "Original")
                self.assertEqual(foreign.read_text(), "Do not remove")
                self.assertEqual(set(self.directory.iterdir()), {self.path, foreign})
                self.assertEqual(event.call_count, isinstance(error, OSError))

    def test_close_failure_preserves_destination_and_cleans_the_temporary(self):
        self.path.write_text("Original", encoding="utf-8")
        original = tempfile.NamedTemporaryFile
        opened = []

        def create(*args, **kwargs):
            stream = original(*args, **kwargs)
            opened.append(stream)
            close = stream.close

            def failed_close():
                close()
                raise OSError("Controlled flush/close failure")

            stream.close = failed_close
            return stream

        with (
            patch.object(files.tempfile, "NamedTemporaryFile", side_effect=create),
            patch.object(files, "event"),
            self.assertRaisesRegex(OSError, "Controlled flush/close failure"),
        ):
            atomic_text(self.path, "Must not publish")
        self.assertTrue(opened[0].closed)
        self.assertEqual(self.path.read_text(), "Original")
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_replace_failure_cleans_temporary_and_preserves_completed_destination(self):
        self.path.write_text("Original", encoding="utf-8")
        observed = []

        def fail(source, destination):
            observed.append(Path(source))
            self.assertEqual(Path(source).read_text(), "New content")
            raise OSError("Controlled replacement failure")

        with (
            patch.object(files.os, "replace", side_effect=fail),
            patch.object(files, "event") as event,
            self.assertRaisesRegex(OSError, "Controlled replacement failure"),
        ):
            atomic_text(self.path, "New content")
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertEqual(self.path.read_text(), "Original")
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        self.assertEqual(event.call_args.args[0], "artifact_write_failed")

    def test_encoding_failure_cleans_temporary_without_replacing_destination(self):
        self.path.write_text("Original", encoding="utf-8")
        with self.assertRaises(UnicodeEncodeError):
            atomic_text(self.path, "invalid surrogate \ud800")
        self.assertEqual(self.path.read_text(), "Original")
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_json_preserves_serialization_and_serialization_errors_preserve_file(self):
        data = {
            "text": "Électricité — 測定",
            "items": [None, True, 1],
            "nested": {"x": 2},
        }
        atomic_json(self.path, data)
        before = self.path.read_bytes()
        self.assertEqual(
            before, json.dumps(data, ensure_ascii=False, indent=2).encode()
        )
        self.assertEqual(json.loads(before), data)
        with self.assertRaises(TypeError):
            atomic_json(self.path, {"unsupported": object()})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_artifact_and_manifest_writes_preserve_external_targets(self):
        config = {"pipeline_version": 2}
        cache = Artifacts(self.directory, config)
        content_link = cache.root / "notes.md.tmp"
        manifest_link = cache.root / "manifest.json.tmp"
        content_link.symlink_to(self.external)
        manifest_link.symlink_to(self.external)
        dependencies = {"source": "verified"}
        cache.write("notes.md", "Électricité [Page 1]", dependencies)
        reloaded = Artifacts(self.directory, config)
        self.assertEqual(
            reloaded.read("notes.md", dependencies), "Électricité [Page 1]"
        )
        self.assertTrue(reloaded.manifest["artifacts"]["notes.md"]["complete"])
        self.assertTrue(content_link.is_symlink())
        self.assertTrue(manifest_link.is_symlink())
        self.assertEqual(self.external.read_text(), "EXTERNAL CONTENT")
        self.assertEqual(
            set(cache.root.iterdir()),
            {content_link, manifest_link, cache.root / "notes.md", cache.path},
        )


if __name__ == "__main__":
    unittest.main()
