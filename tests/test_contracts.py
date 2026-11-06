"""Typed dictionaries keep the existing JSON, snapshots and result semantics."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from document_processor import config, extraction, prompts, reliability
from document_processor.contracts import (
    Chunk,
    CompletionMetrics,
    ExtractionResult,
    ExtractionSettings,
    JobConfig,
    JobSnapshot,
    PageText,
    PublicJobConfig,
)
from document_processor.jobs import JobStore
from document_processor.model_client import ModelClient
from document_processor.services import make_snapshot
from document_processor.settings import Settings

TEXT = "Résumé : température −2,5 °C ; précision ±0,2 %. 中文 العربية 😀"


def defaults():
    with patch.dict(os.environ, {}, clear=True):
        return Settings.from_env().job_defaults()


class CountingTokenizer:
    def tokenize(self, text: str) -> int:
        return len(text)


class ContractTests(unittest.TestCase):
    def snapshot(self, options=None):
        return make_snapshot(defaults(), options, None, lambda: TEXT)

    def test_required_and_optional_fields_match_creation_and_legacy_boundaries(self):
        self.assertIn("extraction", JobConfig.__required_keys__)
        self.assertIn("analysis_tokens", JobConfig.__required_keys__)
        self.assertIn("system_prompt", JobSnapshot.__required_keys__)
        self.assertIn("templates", JobSnapshot.__required_keys__)
        self.assertIn("prompt_version", JobSnapshot.__required_keys__)
        self.assertIn("source_hash", JobSnapshot.__optional_keys__)
        self.assertIn("effective_report_language", JobSnapshot.__optional_keys__)
        self.assertIn("initial_thinking", JobConfig.__optional_keys__)
        self.assertIn("version", ExtractionSettings.__optional_keys__)
        self.assertIn("parts", Chunk.__optional_keys__)
        self.assertEqual(PublicJobConfig.__required_keys__, {"fingerprint"})
        self.assertFalse(CompletionMetrics.__required_keys__)

    def test_new_snapshot_json_round_trip_preserves_profiles_thinking_keys_and_hashes(
        self,
    ):
        for profile in ("legacy", "low", "medium", "high", "ultra"):
            with self.subTest(profile=profile):
                deployment = defaults()
                deployment["thinking_adapter"] = "prompt"
                snapshot = make_snapshot(
                    deployment,
                    {
                        "profile": profile,
                        "overrides": {
                            "thinking_section": "on",
                            "thinking_merge": "off",
                            "thinking_final": "inherit",
                        },
                    },
                    None,
                    lambda: TEXT,
                )
                snapshot["source_hash"] = "original-pdf-hash"
                snapshot["effective_report_language"] = "fr"
                encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
                decoded = json.loads(encoded)
                self.assertIs(type(snapshot), dict)
                self.assertEqual(decoded, snapshot)
                self.assertEqual(
                    config.fingerprint(decoded), config.fingerprint(snapshot)
                )
                self.assertEqual(decoded["profile"], profile)
                self.assertEqual(decoded["schema_version"], 1)
                self.assertEqual(decoded["pipeline_version"], 2)
                self.assertEqual(
                    {
                        key: decoded[key]
                        for key in (
                            "thinking_section",
                            "thinking_merge",
                            "thinking_final",
                        )
                    },
                    {
                        "thinking_section": "on",
                        "thinking_merge": "off",
                        "thinking_final": "inherit",
                    },
                )
                self.assertEqual(
                    prompts.instruction(decoded, "section", "Question"),
                    prompts.instruction(snapshot, "section", "Question"),
                )
                self.assertIn(TEXT, encoded)

    def test_legacy_snapshot_missing_optional_fields_and_unknown_keys_remains_usable(
        self,
    ):
        snapshot = self.snapshot()
        for key in (
            "effective_report_language",
            "initial_thinking",
            "apply_template",
            "usage_supported",
        ):
            snapshot.pop(key, None)
        snapshot["extraction"].pop("version", None)
        snapshot["prompt_version"] = 3
        snapshot["source_hash"] = "legacy-pdf-hash"
        snapshot["historical_extension"] = {"retained": [1, None, "old"]}
        decoded = json.loads(json.dumps(snapshot))
        self.assertEqual(decoded, snapshot)
        self.assertEqual(config.fingerprint(decoded), config.fingerprint(snapshot))
        self.assertEqual(prompts.deterministic_language(decoded), "auto")
        self.assertEqual(
            prompts.instruction(decoded, "final", "Legacy request"),
            prompts.instruction(snapshot, "final", "Legacy request"),
        )
        client = ModelClient(
            "http://unused.invalid", "", decoded, decoded["system_prompt"]
        )
        self.addCleanup(client.session.close)
        self.assertEqual(
            client.messages("Analyze", TEXT),
            [
                {"role": "system", "content": TEXT},
                {"role": "user", "content": "Analyze\n\n" + TEXT},
            ],
        )
        self.assertNotIn("effective_report_language", decoded)
        self.assertNotIn("version", decoded["extraction"])

    def test_saved_legacy_and_current_snapshots_retain_json_and_public_allowlist(self):
        with tempfile.TemporaryDirectory(prefix="contracts-store-") as directory:
            store = JobStore(Path(directory))
            self.addCleanup(store.close)
            store.initialize()
            for number, profile in enumerate(("legacy", "low")):
                snapshot = self.snapshot({"profile": profile})
                snapshot["source_hash"] = f"pdf-{number}"
                if profile == "legacy":
                    snapshot.pop("effective_report_language", None)
                    snapshot["old_private_field"] = {"value": "PRIVATE_LEGACY"}
                identifier = f"{number:032x}"
                expected_hash = config.fingerprint(snapshot)
                store.create_job(identifier, "example.pdf", "Question", 1, snapshot)
                saved = store.load_config(identifier)
                self.assertEqual(saved, snapshot)
                self.assertEqual(config.fingerprint(saved), expected_hash)
                public = config.public_config(saved, expected_hash)
                self.assertEqual(json.loads(json.dumps(public)), public)
                self.assertEqual(public["fingerprint"], expected_hash)
                self.assertEqual(public["profile"], profile)
                self.assertEqual(
                    set(public) - {"fingerprint"}, PublicJobConfig.__optional_keys__
                )
                for private in (
                    "system_prompt",
                    "templates",
                    "source_hash",
                    "extraction",
                    "old_private_field",
                ):
                    self.assertNotIn(private, public)
                self.assertNotIn("PRIVATE_LEGACY", json.dumps(public))

    def test_raw_invalid_options_keep_existing_validation_errors(self):
        invalid = (
            ([], "invalid_options", "Unsupported job options."),
            ({"profile": "unknown"}, "invalid_profile", "Unknown performance profile."),
            (
                {"overrides": {"analysis_tokens": "1200"}},
                "invalid_budget",
                "Invalid analysis_tokens budget.",
            ),
            (
                {"overrides": {"thinking_secton": "on"}},
                "invalid_overrides",
                "Unsupported advanced parameters.",
            ),
        )
        for options, code, message in invalid:
            with (
                self.subTest(options=options),
                self.assertRaises(config.ConfigurationError) as raised,
            ):
                config.resolve(defaults(), options)
            self.assertEqual(
                (raised.exception.code, str(raised.exception)), (code, message)
            )
        with self.assertRaises(config.ConfigurationError) as raised:
            config.parse_options("[]")
        self.assertEqual(
            (raised.exception.code, str(raised.exception)),
            ("invalid_options", "Options must be a JSON object."),
        )

    def test_extraction_pages_chunks_and_artifact_json_keep_original_shapes(self):
        pages: list[PageText] = [
            {"page": 2, "text": TEXT},
            {"page": 9, "text": "Physical page nine, including a quoted [Page 99]."},
        ]
        result: ExtractionResult = {
            "pages": pages,
            "page_count": 9,
            "missing": [4],
            "ocr_pages": [2],
        }
        chunks = extraction.make_chunks(pages, CountingTokenizer(), 150)
        self.assertTrue(chunks)
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)
        self.assertEqual(json.loads(json.dumps(chunks, ensure_ascii=False)), chunks)
        self.assertEqual(
            "".join(part["text"] for chunk in chunks for part in chunk["parts"]),
            "".join(page["text"] for page in pages),
        )
        self.assertEqual(
            sorted({page for chunk in chunks for page in chunk["pages"]}), [2, 9]
        )
        # The extraction API stays a tuple; JSON preserves its established list encoding.
        legacy_tuple = (pages, 9, [4], [2])
        self.assertEqual(json.loads(json.dumps(legacy_tuple)), [pages, 9, [4], [2]])
        legacy_chunk = {"text": TEXT, "pages": [2]}
        self.assertEqual(json.loads(json.dumps(legacy_chunk)), legacy_chunk)
        self.assertNotIn("parts", legacy_chunk)

    def test_note_json_preserves_provenance_and_optional_historical_warnings(self):
        first = reliability.note(TEXT, [9, 2, 2], [1, 0], ["missing_quantity"])
        legacy = {"text": "Old note", "pages": [3], "sections": [2]}
        combined = reliability.merge_note(
            "Merged", json.loads(json.dumps([first, legacy]))
        )
        self.assertEqual(
            combined,
            {
                "text": "Merged",
                "pages": [2, 3, 9],
                "sections": [0, 1, 2],
                "warning_codes": ["missing_quantity"],
            },
        )
        self.assertEqual(json.loads(json.dumps(first, ensure_ascii=False)), first)

    def test_generation_metrics_stay_plain_dicts_with_unknown_values_and_shared_identity(
        self,
    ):
        client = ModelClient("http://unused.invalid", "", defaults(), TEXT)
        self.addCleanup(client.session.close)
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_lines.return_value = iter(
            [
                b"data: "
                + json.dumps(
                    {
                        "choices": [
                            {"delta": {"content": TEXT}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 17, "completion_tokens": 4},
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                b"data: [DONE]",
            ]
        )
        with (
            patch.object(
                client, "fits", return_value=(True, 20, "estimated_template", 384)
            ),
            patch.object(client, "model", return_value="fake"),
            patch.object(client.session, "post", return_value=response),
        ):
            completed = client.generate("Analyze", TEXT, 100)
        self.assertEqual(completed.content, TEXT)
        self.assertIs(type(completed.metrics), dict)
        self.assertIs(completed.metrics, client.current_metrics)
        self.assertIs(client.last_result, completed)
        self.assertEqual(
            json.loads(json.dumps(completed.metrics, allow_nan=False)),
            completed.metrics,
        )
        self.assertIs(type(completed.metrics["output_tokens"]), int)
        self.assertIsNone(completed.metrics["reasoning_tokens"])
        self.assertEqual(completed.metrics["finish_reason"], "stop")
        self.assertEqual(completed.metrics["output_tokens_source"], "engine")
        self.assertNotIn(TEXT, json.dumps(completed.metrics))


if __name__ == "__main__":
    unittest.main()
