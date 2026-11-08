"""Deployment parsing, immutable values and the app compatibility boundary."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path
from unittest.mock import patch

from document_processor import config, environment
from document_processor.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "app_port": 9932,
    "data_dir": Path("/app/data"),
    "llama_base_url": "http://host.docker.internal:8080",
    "api_key": "",
    "max_upload_mb": 20,
    "max_pages": 120,
    "chunk_tokens": 3000,
    "context_tokens": 16384,
    "output_language": "auto",
    "analysis_tokens": 1000,
    "merge_tokens": 1200,
    "final_tokens": 1800,
    "request_timeout": 21600,
    "step_timeout": 21600,
    "max_model_calls": 500,
    "max_job_attempts": 10,
    "live_output_enabled": True,
    "live_preview_chars": 12000,
    "ocr_enabled": True,
    "ocr_languages": "eng+fra",
    "ocr_dpi": 150,
    "ocr_max_pixels": 5000000,
    "ocr_timeout": 180,
    "thinking_adapter": "unsupported",
    "initial_thinking": False,
    "apply_template_supported": False,
    "usage_supported": False,
    "system_prompt_file": Path("/app/config/system_prompt.txt"),
}


def from_env(**overrides):
    """Ignore the machine's deployment variables for each parsing example."""
    with patch.dict(os.environ, overrides, clear=True):
        return Settings.from_env()


class SettingsTests(unittest.TestCase):
    def test_defaults_and_types(self):
        settings = from_env()
        self.assertEqual(asdict(settings), DEFAULTS)
        for name, expected in DEFAULTS.items():
            with self.subTest(field=name):
                self.assertIs(type(getattr(settings, name)), type(expected))
        self.assertEqual(settings.max_upload_bytes, 20 * 1024 * 1024)
        self.assertEqual(settings.max_content_length, 20 * 1024 * 1024 + 65536)
        self.assertEqual(config.resolve(settings.job_defaults())["profile"], "legacy")

    def test_overrides_cover_every_deployment_value(self):
        settings = from_env(
            APP_PORT="8123",
            DATA_DIR="relative data",
            LLAMA_BASE_URL=" https://user:PRIVATE_URL@backend.invalid/base/// ",
            LLAMA_API_KEY="  PRIVATE_KEY  ",
            MAX_UPLOAD_MB="3",
            MAX_PAGES="321",
            CHUNK_TOKENS="4000",
            CONTEXT_TOKENS="32768",
            OUTPUT_LANGUAGE=" fr ",
            ANALYSIS_TOKENS="1200",
            MERGE_TOKENS="1400",
            FINAL_TOKENS="2000",
            REQUEST_TIMEOUT_SECONDS="60",
            STEP_TIMEOUT_SECONDS="45",
            MAX_MODEL_CALLS="23",
            MAX_JOB_ATTEMPTS="4",
            LIVE_OUTPUT_ENABLED="off",
            LIVE_PREVIEW_CHARS="3456",
            OCR_ENABLED="false",
            OCR_LANGUAGES=" eng ",
            OCR_DPI="200",
            OCR_MAX_PIXELS="7654321",
            OCR_TIMEOUT_SECONDS="65",
            LLAMA_THINKING_ADAPTER="prompt",
            LLAMA_INITIAL_THINKING="yes",
            LLAMA_APPLY_TEMPLATE_SUPPORTED="on",
            LLAMA_USAGE_SUPPORTED="1",
            SYSTEM_PROMPT_FILE="relative config/prompt.txt",
        )
        expected = {
            "app_port": 8123,
            "data_dir": Path("relative data"),
            "llama_base_url": "https://user:PRIVATE_URL@backend.invalid/base",
            "api_key": "  PRIVATE_KEY  ",
            "max_upload_mb": 3,
            "max_pages": 321,
            "chunk_tokens": 4000,
            "context_tokens": 32768,
            "output_language": "fr",
            "analysis_tokens": 1200,
            "merge_tokens": 1400,
            "final_tokens": 2000,
            "request_timeout": 60,
            "step_timeout": 45,
            "max_model_calls": 23,
            "max_job_attempts": 4,
            "live_output_enabled": False,
            "live_preview_chars": 3456,
            "ocr_enabled": False,
            "ocr_languages": "eng",
            "ocr_dpi": 200,
            "ocr_max_pixels": 7654321,
            "ocr_timeout": 65,
            "thinking_adapter": "prompt",
            "initial_thinking": True,
            "apply_template_supported": True,
            "usage_supported": True,
            "system_prompt_file": Path("relative config/prompt.txt"),
        }
        self.assertEqual(asdict(settings), expected)
        self.assertEqual(settings.max_upload_bytes, 3 * 1024 * 1024)
        resolved = config.resolve(settings.job_defaults())
        self.assertEqual(resolved["thinking_section"], "inherit")
        self.assertEqual(resolved["extraction"]["languages"], "eng")

    def test_empty_values_and_timeout_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            Settings.from_env()
            names = [
                "APP_PORT",
                "DATA_DIR",
                "LLAMA_BASE_URL",
                "LLAMA_API_KEY",
                "MAX_UPLOAD_MB",
                "MAX_PAGES",
                "CHUNK_TOKENS",
                "CONTEXT_TOKENS",
                "OUTPUT_LANGUAGE",
                "ANALYSIS_TOKENS",
                "MERGE_TOKENS",
                "FINAL_TOKENS",
                "REQUEST_TIMEOUT_SECONDS",
                "STEP_TIMEOUT_SECONDS",
                "MAX_MODEL_CALLS",
                "MAX_JOB_ATTEMPTS",
                "LIVE_OUTPUT_ENABLED",
                "LIVE_PREVIEW_CHARS",
                "OCR_ENABLED",
                "OCR_LANGUAGES",
                "OCR_DPI",
                "OCR_MAX_PIXELS",
                "OCR_TIMEOUT_SECONDS",
                "LLAMA_THINKING_ADAPTER",
                "LLAMA_INITIAL_THINKING",
                "LLAMA_APPLY_TEMPLATE_SUPPORTED",
                "LLAMA_USAGE_SUPPORTED",
                "SYSTEM_PROMPT_FILE",
            ]
        self.assertEqual(asdict(from_env(**dict.fromkeys(names, ""))), DEFAULTS)
        for step in (None, "", "  "):
            values = {"REQUEST_TIMEOUT_SECONDS": "81"}
            if step is not None:
                values["STEP_TIMEOUT_SECONDS"] = step
            with self.subTest(step=step):
                self.assertEqual(from_env(**values).step_timeout, 81)
        self.assertEqual(
            from_env(
                REQUEST_TIMEOUT_SECONDS="81", STEP_TIMEOUT_SECONDS="9"
            ).step_timeout,
            9,
        )

    def test_paths_and_credentials_retain_original_whitespace_without_expansion(self):
        for raw in ("relative/path", "~/private", "  "):
            with self.subTest(path=raw):
                settings = from_env(
                    DATA_DIR=raw, SYSTEM_PROMPT_FILE=raw, LLAMA_API_KEY=raw
                )
                self.assertEqual(settings.data_dir, Path(raw))
                self.assertEqual(settings.system_prompt_file, Path(raw))
                self.assertEqual(settings.api_key, raw)
        self.assertEqual(
            from_env(OUTPUT_LANGUAGE="  ", OCR_LANGUAGES="  ").output_language, "auto"
        )
        self.assertEqual(from_env(OCR_LANGUAGES="  ").ocr_languages, "eng+fra")
        self.assertEqual(from_env(OUTPUT_LANGUAGE="French").output_language, "French")

    def test_live_preview_clamps_positive_values_to_the_existing_range(self):
        for raw, expected in (
            ("1", 1000),
            ("999", 1000),
            ("1000", 1000),
            ("12000", 12000),
            ("50000", 50000),
            ("50001", 50000),
        ):
            with self.subTest(value=raw):
                self.assertEqual(
                    from_env(LIVE_PREVIEW_CHARS=raw).live_preview_chars, expected
                )

    def test_invalid_integer_messages_are_preserved_for_every_numeric_variable(self):
        variables = (
            "APP_PORT",
            "MAX_UPLOAD_MB",
            "MAX_PAGES",
            "CHUNK_TOKENS",
            "CONTEXT_TOKENS",
            "ANALYSIS_TOKENS",
            "MERGE_TOKENS",
            "FINAL_TOKENS",
            "REQUEST_TIMEOUT_SECONDS",
            "STEP_TIMEOUT_SECONDS",
            "MAX_MODEL_CALLS",
            "MAX_JOB_ATTEMPTS",
            "LIVE_PREVIEW_CHARS",
            "OCR_DPI",
            "OCR_MAX_PIXELS",
            "OCR_TIMEOUT_SECONDS",
        )
        for name in variables:
            for value in ("bad", "0", "-1"):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(environment.SettingsError) as raised,
                ):
                    from_env(**{name: value})
                bounds = "between 1 and 65535" if name == "APP_PORT" else "at least 1"
                self.assertEqual(
                    str(raised.exception),
                    f"{name} must be "
                    + ("an integer." if value == "bad" else bounds + "."),
                )
        with self.assertRaises(environment.SettingsError) as raised:
            from_env(APP_PORT="65536")
        self.assertEqual(str(raised.exception), "APP_PORT must be between 1 and 65535.")
        self.assertEqual(from_env(APP_PORT="65535").app_port, 65535)

    def test_invalid_flags_adapter_and_url_do_not_echo_values(self):
        for name in (
            "LIVE_OUTPUT_ENABLED",
            "OCR_ENABLED",
            "LLAMA_INITIAL_THINKING",
            "LLAMA_APPLY_TEMPLATE_SUPPORTED",
            "LLAMA_USAGE_SUPPORTED",
        ):
            with (
                self.subTest(name=name),
                self.assertRaises(environment.SettingsError) as raised,
            ):
                from_env(**{name: "PRIVATE_INVALID_FLAG"})
            self.assertEqual(
                str(raised.exception),
                f"{name} must be one of: 1, true, yes, on, 0, false, no, off.",
            )
        with self.assertRaises(environment.SettingsError) as raised:
            from_env(LLAMA_THINKING_ADAPTER="PRIVATE_ADAPTER")
        self.assertEqual(
            str(raised.exception),
            "LLAMA_THINKING_ADAPTER must be one of: unsupported, chat_template, prompt.",
        )
        with self.assertRaises(environment.SettingsError) as raised:
            from_env(LLAMA_BASE_URL="ftp://user:PRIVATE_KEY@host")
        self.assertEqual(
            str(raised.exception), "LLAMA_BASE_URL must be an http:// or https:// URL."
        )

    def test_first_invalid_variable_keeps_the_existing_parsing_order(self):
        with self.assertRaises(environment.SettingsError) as raised:
            from_env(
                APP_PORT="bad", LLAMA_BASE_URL="bad", REQUEST_TIMEOUT_SECONDS="bad"
            )
        self.assertEqual(str(raised.exception), "APP_PORT must be an integer.")

    def test_job_limit_and_cross_budget_checks_remain_in_config_resolve(self):
        cases = (
            ({"CHUNK_TOKENS": "127"}, "invalid_budget", "Invalid chunk_tokens budget."),
            ({"MAX_JOB_ATTEMPTS": "101"}, "invalid_budget", "Invalid retry limit."),
            ({"OCR_DPI": "601"}, "invalid_extraction", "Invalid extraction limits."),
            (
                {"OCR_LANGUAGES": "bad/language"},
                "invalid_extraction",
                "Invalid OCR languages.",
            ),
            (
                {"CONTEXT_TOKENS": "511"},
                "invalid_context",
                "Invalid application context limit.",
            ),
            (
                {"CONTEXT_TOKENS": "1024"},
                "context_insufficient",
                "Budgets exceed the available context; reduce them before starting.",
            ),
        )
        for values, code, message in cases:
            with self.subTest(values=values):
                settings = from_env(**values)
                with self.assertRaises(config.ConfigurationError) as raised:
                    config.resolve(settings.job_defaults())
                self.assertEqual(
                    (raised.exception.code, str(raised.exception)), (code, message)
                )

    def test_settings_are_frozen_and_job_defaults_have_no_shared_mutable_state(self):
        settings = from_env()
        with self.assertRaises(FrozenInstanceError):
            settings.max_upload_mb = 1
        with self.assertRaises(FrozenInstanceError):
            del settings.data_dir
        self.assertFalse(hasattr(settings, "__dict__"))
        for field in fields(settings):
            self.assertIsInstance(getattr(settings, field.name), (str, int, bool, Path))
        changed = replace(settings, analysis_tokens=1500)
        self.assertEqual(settings.analysis_tokens, 1000)
        self.assertEqual(changed.analysis_tokens, 1500)
        first = settings.job_defaults()
        first["analysis_tokens"] = 99
        first["extraction"]["dpi"] = 99
        self.assertEqual(settings.job_defaults()["analysis_tokens"], 1000)
        self.assertEqual(settings.job_defaults()["extraction"]["dpi"], 150)

    def test_repr_and_public_defaults_exclude_credentials_and_paths(self):
        settings = from_env(
            LLAMA_API_KEY="PRIVATE_KEY",
            LLAMA_BASE_URL="https://user:PRIVATE_URL@host",
            DATA_DIR="PRIVATE_DATA",
            SYSTEM_PROMPT_FILE="PRIVATE_PROMPT",
        )
        defaults = settings.job_defaults()
        public = config.public_config(config.resolve(defaults))
        for output in (repr(settings), json.dumps(defaults), json.dumps(public)):
            self.assertNotIn("PRIVATE_", output)
        self.assertNotIn("api_key", defaults)
        self.assertNotIn("system_prompt", defaults)

    def test_reading_values_does_not_touch_files_services_or_logging(self):
        with (
            patch.object(Path, "read_text", side_effect=AssertionError("No files")),
            patch.object(Path, "mkdir", side_effect=AssertionError("No directories")),
            patch("requests.Session", side_effect=AssertionError("No HTTP")),
            patch("sqlite3.connect", side_effect=AssertionError("No database")),
            patch("threading.Thread.start", side_effect=AssertionError("No worker")),
            patch("logging.basicConfig", side_effect=AssertionError("No logging")),
        ):
            settings = from_env(
                DATA_DIR="does/not/exist", SYSTEM_PROMPT_FILE="missing.txt"
            )
            self.assertEqual(settings.system_prompt_file, Path("missing.txt"))

    def test_import_does_not_read_environment_or_initialize_application_services(self):
        code = """
import dataclasses, logging, os, pathlib, sqlite3, sys, threading
import requests
from unittest.mock import patch
with patch('os.getenv', side_effect=AssertionError('Environment read at import')), \
     patch.object(pathlib.Path, 'read_text', side_effect=AssertionError('File read at import')), \
     patch('requests.Session', side_effect=AssertionError('HTTP at import')), \
     patch('sqlite3.connect', side_effect=AssertionError('Database at import')), \
     patch('threading.Thread.start', side_effect=AssertionError('Worker at import')), \
     patch('logging.basicConfig', side_effect=AssertionError('Logging at import')):
    import document_processor.settings
assert 'app' not in sys.modules
assert not {'document_processor.jobs', 'document_processor.pipeline', 'document_processor.model_client'} & sys.modules.keys()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class AppSettingsCompatibilityTests(unittest.TestCase):
    def test_legacy_globals_adapt_every_field_without_mutating_initial_settings(self):
        import app

        initial = app.SETTINGS
        aliases = {
            "data_dir": "DATA",
            "llama_base_url": "LLAMA_BASE",
            "api_key": "KEY",
            "system_prompt_file": "SYSTEM_FILE",
        }
        for field in fields(initial):
            name = aliases.get(field.name, field.name.upper())
            previous = getattr(app, name)
            value = (
                not previous
                if isinstance(previous, bool)
                else previous + 1
                if isinstance(previous, int)
                else Path("override/path")
                if isinstance(previous, Path)
                else previous + "_override"
            )
            with self.subTest(name=name), patch.object(app, name, value):
                self.assertEqual(getattr(app.deployment_settings(), field.name), value)
                self.assertIs(app.SETTINGS, initial)
                self.assertEqual(getattr(initial, field.name), previous)
        self.assertEqual(app.deployment_settings(), initial)

    def test_explicit_settings_control_clients_and_snapshots_without_global_patches(
        self,
    ):
        import app

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system_prompt.txt"
            path.write_text("  Private editable prompt  ", encoding="utf-8")
            settings = replace(
                from_env(),
                system_prompt_file=path,
                llama_base_url="http://explicit.invalid",
                api_key="PRIVATE_KEY",
                analysis_tokens=1500,
                live_preview_chars=2345,
            )
            with (
                patch.object(app, "ANALYSIS_TOKENS", 99),
                patch.object(app, "SYSTEM_FILE", Path("missing.txt")),
            ):
                self.assertEqual(
                    app.runtime_defaults(settings)["analysis_tokens"], 1500
                )
                snapshot = app.new_snapshot({}, settings=settings)
                self.assertEqual(snapshot["system_prompt"], "Private editable prompt")
                client = app.ModelClient(settings=settings)
                self.addCleanup(client.session.close)
                self.assertEqual(client.base_url, "http://explicit.invalid")
                self.assertEqual(
                    client.session.headers["Authorization"], "Bearer PRIVATE_KEY"
                )
                self.assertEqual(client.prompt, "Private editable prompt")
                self.assertEqual(client.preview_limit, 2345)
                self.assertEqual(client.config["analysis_tokens"], 1500)
                self.assertNotIn("PRIVATE_KEY", json.dumps(snapshot))

    def test_prompt_edits_affect_only_new_snapshots_and_do_not_reread_environment(self):
        import app

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system_prompt.txt"
            settings = replace(from_env(), system_prompt_file=path)
            path.write_text("First prompt", encoding="utf-8")
            with (
                patch.object(app, "SYSTEM_FILE", path),
                patch(
                    "os.getenv",
                    side_effect=AssertionError("Settings must not be reread"),
                ),
            ):
                first = app.new_snapshot({})
                path.write_text("  Second prompt  ", encoding="utf-8")
                second = app.new_snapshot({})
                self.assertEqual(app.read_system_prompt(settings), "Second prompt")
                self.assertEqual(first["system_prompt"], "First prompt")
                self.assertEqual(second["system_prompt"], "Second prompt")
                path.write_text("   ", encoding="utf-8")
                with self.assertRaises(config.ConfigurationError) as empty:
                    app.new_snapshot({})
                self.assertEqual(
                    (empty.exception.code, str(empty.exception)),
                    ("missing_prompt", "The system prompt is empty."),
                )
                path.unlink()
                with self.assertRaises(config.ConfigurationError) as missing:
                    app.read_system_prompt()
                self.assertEqual(
                    (missing.exception.code, str(missing.exception)),
                    ("missing_prompt", "The system prompt is missing or unreadable."),
                )

    def test_no_argument_app_hooks_remain_usable(self):
        import app

        defaults = from_env().job_defaults()
        with (
            patch.object(app, "runtime_defaults", lambda: defaults),
            patch.object(app, "read_system_prompt", lambda: "Hook prompt"),
        ):
            snapshot = app.new_snapshot({})
            client = app.ModelClient()
            self.addCleanup(client.session.close)
        self.assertEqual(snapshot["system_prompt"], "Hook prompt")
        self.assertEqual(client.prompt, "Hook prompt")

    def test_routes_use_legacy_limit_overrides_without_reloading_environment(self):
        import app

        with (
            patch.object(app, "MAX_UPLOAD_MB", 7),
            patch(
                "document_processor.settings.Settings.from_env",
                side_effect=AssertionError("No environment reload"),
            ),
        ):
            client = app.app.test_client()
            self.assertIn("7 MiB", client.get("/").get_data(as_text=True))
            response = client.get("/api/profiles")
            self.assertEqual(response.status_code, 200)
            with app.app.test_request_context():
                body, status = app.too_large(None)
            self.assertEqual(status, 413)
            self.assertEqual(body.json["error"], "PDF too large (limit: 7 MiB)")


if __name__ == "__main__":
    unittest.main()
