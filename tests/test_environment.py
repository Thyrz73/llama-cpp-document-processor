"""Validate environment settings, startup and shutdown, and keep code defaults aligned with Compose."""

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests

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


class ShutdownTests(unittest.TestCase):
    @staticmethod
    def free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def wait_until(self, condition, server, timeout=30):
        """Poll condition until it is true; fail at once if the server exits first."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.poll() is not None:
                self.fail(f"app.py exited with status {server.returncode}")
            try:
                if condition():
                    return
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.1)
        self.fail("timed out waiting for app.py")

    @unittest.skipUnless(os.name == "posix", "SIGTERM is a POSIX signal")
    def test_stop_signal_leaves_every_committed_change_in_the_database_file(self):
        with tempfile.TemporaryDirectory(prefix="shutdown-test-") as data:
            # The backend port is closed: the job fails at once, so the web thread and the worker both write.
            env = {key: value for key, value in os.environ.items() if key not in COMPOSE_DEFAULTS}
            port = self.free_port()
            env.update(DATA_DIR=data, APP_PORT=str(port), LLAMA_BASE_URL=f"http://127.0.0.1:{self.free_port()}",
                       SYSTEM_PROMPT_FILE=str(ROOT / "config/system_prompt.example.txt"),
                       NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
            http = requests.Session()
            http.trust_env = False  # the server is local: ignore any proxy variable
            self.addCleanup(http.close)
            server = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"http://127.0.0.1:{port}"
                self.wait_until(lambda: http.get(base + "/api/health", timeout=2).ok, server)
                created = http.post(base + "/api/jobs", data={"question": "Summarize"}, timeout=10,
                                    files={"file": ("a.pdf", b"%PDF-1.4")})
                self.assertEqual(created.status_code, 201)
                job = f"{base}/api/jobs/{created.json()['id']}"
                self.wait_until(lambda: http.get(job, timeout=2).json()["status"] == "failed", server)
                server.send_signal(signal.SIGTERM)
                self.assertEqual(server.wait(timeout=30), 0)
            finally:
                if server.poll() is None:
                    server.kill()
                    server.wait()
            # Copy the database file alone: its content must not depend on the -wal and -shm files.
            copy = Path(data) / "copy.sqlite3"
            shutil.copy(Path(data) / "jobs.sqlite3", copy)
            with closing(sqlite3.connect(copy)) as con:
                self.assertEqual(con.execute("SELECT status FROM jobs").fetchall(), [("failed",)])

    @unittest.skipUnless(os.name == "posix", "SIGTERM is a POSIX signal")
    def test_sigterm_during_worker_transaction_waits_for_commit_before_main_checkpoint(self):
        script = """
import os, runpy, signal, threading
import waitress

def serve(app, **kwargs):
    context = app.view_functions['health'].__globals__
    store, worker = context['store'], context['worker']
    entered = threading.Event()
    def run(job_id, check):
        store.update_job(job_id, progress_percent=25)
        with store.connect() as con:
            con.execute('UPDATE jobs SET progress_percent=41 WHERE id=?', (job_id,))
            entered.set()
            if not worker._stopping.wait(3):
                raise RuntimeError('Test did not request shutdown')
        check(job_id)
    worker.run_job = run
    store.create_job('a' * 32, 'test.pdf', 'Summarize', 1, {'max_attempts': 3})
    worker.wake()
    if not entered.wait(3):
        raise RuntimeError('Worker did not start the test transaction')
    os.kill(os.getpid(), signal.SIGTERM)

waitress.serve = serve
runpy.run_path('app.py', run_name='__main__')
"""
        with tempfile.TemporaryDirectory(prefix="shutdown-transaction-test-") as data:
            env = {key: value for key, value in os.environ.items() if key not in COMPOSE_DEFAULTS}
            env.update(DATA_DIR=data, SYSTEM_PROMPT_FILE=str(ROOT / "config/system_prompt.example.txt"))
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 8)
            self.assertIn('worker_stopped=true', result.stderr)
            self.assertNotIn("worker_shutdown_timeout", result.stderr)
            copy = Path(data) / "copy.sqlite3"
            shutil.copy(Path(data) / "jobs.sqlite3", copy)
            with closing(sqlite3.connect(copy)) as con:
                self.assertEqual(con.execute("SELECT status,progress_percent,cancel_requested FROM jobs").fetchall(),
                                 [("running", 41, 0)])
            from document_processor.jobs import JobStore
            store = JobStore(data)
            try:
                store.initialize()
                self.assertEqual(store.get_job("a" * 32)["status"], "queued")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
