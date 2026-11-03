"""Validate environment settings and keep code defaults aligned with Compose."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processor import environment

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DEFAULTS = dict(re.findall(r"^\s+([A-Z_]+): \$\{[A-Z_]+:-([^}]*)\}",
                                   (ROOT / "compose.yaml").read_text(encoding="utf-8"), re.M))


def run_app(code, **env):
    """Import or start app.py in a clean process without Compose variables."""
    clean = {key: value for key, value in os.environ.items() if key not in COMPOSE_DEFAULTS}
    clean.update(DATA_DIR=tempfile.mkdtemp(prefix="environment-test-"),
                 SYSTEM_PROMPT_FILE=str(ROOT / "config/system_prompt.example.txt"), **env)
    return subprocess.run([sys.executable, *code], cwd=ROOT, env=clean, capture_output=True,
                          text=True, timeout=120, check=False)


class SettingsHelperTests(unittest.TestCase):
    def test_integer(self):
        with patch.dict(os.environ, {"X": ""}):
            self.assertEqual(environment.integer("X", 7), 7)
        with patch.dict(os.environ, {"X": " 42 "}):
            self.assertEqual(environment.integer("X", 7), 42)
        for value, message in (("abc", "X must be an integer."), ("0", "X must be between 1 and 65535."),
                               ("70000", "X must be between 1 and 65535.")):
            with self.subTest(value=value), patch.dict(os.environ, {"X": value}):
                with self.assertRaisesRegex(environment.SettingsError, re.escape(message)):
                    environment.integer("X", 7, maximum=65535)
        with patch.dict(os.environ, {"X": "-3"}), self.assertRaisesRegex(environment.SettingsError, "X must be at least 1."):
            environment.integer("X", 7)

    def test_flag(self):
        for value, expected in (("TRUE", True), ("on", True), ("0", False), ("No", False), ("", True)):
            with self.subTest(value=value), patch.dict(os.environ, {"X": value}):
                self.assertIs(environment.flag("X", True), expected)
        with patch.dict(os.environ, {"X": "ture"}), self.assertRaisesRegex(environment.SettingsError, "^X must be one of"):
            environment.flag("X", True)

    def test_choice_and_url(self):
        with patch.dict(os.environ, {"X": "prompt"}):
            self.assertEqual(environment.choice("X", "a", ("a", "prompt")), "prompt")
        with patch.dict(os.environ, {"X": "other"}), self.assertRaises(environment.SettingsError):
            environment.choice("X", "a", ("a", "prompt"))
        with patch.dict(os.environ, {"X": "https://host:8080/"}):
            self.assertEqual(environment.http_url("X", "http://default"), "https://host:8080")
        for value in ("localhost:8080", "ftp://user:secret@host"):
            with self.subTest(value=value), patch.dict(os.environ, {"X": value}):
                with self.assertRaises(environment.SettingsError) as raised:
                    environment.http_url("X", "http://default")
                self.assertNotIn("secret", str(raised.exception))


class StartupTests(unittest.TestCase):
    def test_invalid_variable_stops_with_a_clear_message(self):
        result = run_app(["-c", "import app"], OCR_DPI="abc")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Configuration error: OCR_DPI must be an integer.", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_inconsistent_budgets_stop_before_serving(self):
        result = run_app(["app.py"], CONTEXT_TOKENS="1024")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Configuration error: Budgets exceed the available context", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_code_defaults_match_compose_defaults(self):
        names = {"APP_PORT": "APP_PORT", "LLAMA_BASE_URL": "LLAMA_BASE", "LLAMA_API_KEY": "KEY",
                 "MAX_UPLOAD_MB": "MAX_UPLOAD_MB", "MAX_PAGES": "MAX_PAGES", "CHUNK_TOKENS": "CHUNK_TOKENS",
                 "CONTEXT_TOKENS": "CONTEXT_TOKENS", "OUTPUT_LANGUAGE": "OUTPUT_LANGUAGE",
                 "ANALYSIS_TOKENS": "ANALYSIS_TOKENS", "MERGE_TOKENS": "MERGE_TOKENS",
                 "FINAL_TOKENS": "FINAL_TOKENS", "REQUEST_TIMEOUT_SECONDS": "REQUEST_TIMEOUT",
                 "STEP_TIMEOUT_SECONDS": "STEP_TIMEOUT", "MAX_MODEL_CALLS": "MAX_MODEL_CALLS",
                 "MAX_JOB_ATTEMPTS": "MAX_JOB_ATTEMPTS", "LIVE_OUTPUT_ENABLED": "LIVE_OUTPUT_ENABLED",
                 "LIVE_PREVIEW_CHARS": "LIVE_PREVIEW_CHARS", "OCR_ENABLED": "OCR_ENABLED",
                 "OCR_LANGUAGES": "OCR_LANGUAGES", "OCR_DPI": "OCR_DPI", "OCR_MAX_PIXELS": "OCR_MAX_PIXELS",
                 "OCR_TIMEOUT_SECONDS": "OCR_TIMEOUT", "LLAMA_THINKING_ADAPTER": "THINKING_ADAPTER",
                 "LLAMA_INITIAL_THINKING": "INITIAL_THINKING",
                 "LLAMA_APPLY_TEMPLATE_SUPPORTED": "APPLY_TEMPLATE_SUPPORTED",
                 "LLAMA_USAGE_SUPPORTED": "USAGE_SUPPORTED"}
        script = ("import json, app; print(json.dumps({k: getattr(app, v) for k, v in "
                  + repr(names) + ".items()}))")
        result = run_app(["-c", script])
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout.strip().splitlines()[-1])
        for name, value in values.items():
            with self.subTest(name=name):
                self.assertIn(name, COMPOSE_DEFAULTS, "variable missing from compose.yaml")
                expected = COMPOSE_DEFAULTS[name]
                self.assertEqual(str(value).lower() if isinstance(value, bool) else str(value), expected)


if __name__ == "__main__":
    unittest.main()
