"""Analyze PDFs with llama.cpp, a queue, and local OCR.

The Flask routes expose the interface, jobs, and reports. The worker
processes one document at a time; ModelClient communicates with llama.cpp,
and ThinkingSplitter separates final text from reasoning tags. SQLite stores
job states, while DATA contains job files.

No embeddings, Redis, GPU, or additional language models are used.

Options are read from the environment when this module is imported.
Running ``python app.py`` initializes the database, starts the worker, and
serves the API with Waitress. Importing the module alone starts neither the
server nor the worker.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
import logging
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from document_processor import config as job_config, jobs as job_store, profiles, prompts, pipeline
from document_processor import environment, reports
from document_processor.observability import configure_logging, current_job_id, event, failure, job_context
from document_processor.errors import ProcessingError
from document_processor.model_client import ModelClient as BackendClient, Cancelled
from flask import Flask, abort, jsonify, render_template, request, send_file, g, make_response
from urllib.parse import urlsplit
from waitress import serve
from werkzeug.exceptions import Conflict, NotFound, RequestEntityTooLarge

# Read the configuration once when the process starts.
# Docker Compose passes variables from its .env file to the container.
# config.resolve() repeats the job-level range checks at startup.
try:
    APP_PORT = environment.integer("APP_PORT", 9932, maximum=65535)
    DATA = Path(os.getenv("DATA_DIR") or "/app/data")
    LLAMA_BASE = environment.http_url("LLAMA_BASE_URL", "http://host.docker.internal:8080")
    KEY = os.getenv("LLAMA_API_KEY", "")
    MAX_UPLOAD_MB = environment.integer("MAX_UPLOAD_MB", 20)
    MAX_PAGES = environment.integer("MAX_PAGES", 120)
    CHUNK_TOKENS = environment.integer("CHUNK_TOKENS", 3000)
    CONTEXT_TOKENS = environment.integer("CONTEXT_TOKENS", 16384)
    OUTPUT_LANGUAGE = os.getenv("OUTPUT_LANGUAGE", "auto").strip() or "auto"
    ANALYSIS_TOKENS = environment.integer("ANALYSIS_TOKENS", 1000)
    MERGE_TOKENS = environment.integer("MERGE_TOKENS", 1200)
    FINAL_TOKENS = environment.integer("FINAL_TOKENS", 1800)
    REQUEST_TIMEOUT = environment.integer("REQUEST_TIMEOUT_SECONDS", 21600)
    STEP_TIMEOUT = environment.integer("STEP_TIMEOUT_SECONDS", REQUEST_TIMEOUT)
    MAX_MODEL_CALLS = environment.integer("MAX_MODEL_CALLS", 500)
    MAX_JOB_ATTEMPTS = environment.integer("MAX_JOB_ATTEMPTS", 10)
    LIVE_OUTPUT_ENABLED = environment.flag("LIVE_OUTPUT_ENABLED", True)
    LIVE_PREVIEW_CHARS = max(1000, min(environment.integer("LIVE_PREVIEW_CHARS", 12000), 50000))
    OCR_ENABLED = environment.flag("OCR_ENABLED", True)
    OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "").strip() or "eng+fra"
    OCR_DPI = environment.integer("OCR_DPI", 150)
    OCR_MAX_PIXELS = environment.integer("OCR_MAX_PIXELS", 5000000)
    OCR_TIMEOUT = environment.integer("OCR_TIMEOUT_SECONDS", 180)
    THINKING_ADAPTER = environment.choice("LLAMA_THINKING_ADAPTER", "unsupported",
                                       ("unsupported", "chat_template", "prompt"))
    INITIAL_THINKING = environment.flag("LLAMA_INITIAL_THINKING", False)
    APPLY_TEMPLATE_SUPPORTED = environment.flag("LLAMA_APPLY_TEMPLATE_SUPPORTED", False)
    USAGE_SUPPORTED = environment.flag("LLAMA_USAGE_SUPPORTED", False)
    SYSTEM_FILE = Path(os.getenv("SYSTEM_PROMPT_FILE") or "/app/config/system_prompt.txt")
except environment.SettingsError as exc:
    raise SystemExit(f"Configuration error: {exc}") from None
MAX_QUESTION_CHARS = 1500
DB_PATH = DATA / "jobs.sqlite3"

configure_logging()
log = logging.getLogger("document-processor")


class ProcessorFlask(Flask):
    def log_exception(self, exc_info):
        """Prevent Flask's default traceback logger from leaking exception bodies."""
        failure("request_failed", exc_info[1], endpoint=request.endpoint)


app = ProcessorFlask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024 + 65536
worker_wakeup = threading.Event()
TRANSLATIONS = json.loads((Path(__file__).parent / "static" / "translations.json").read_text(encoding="utf-8"))
DEFAULT_QUESTIONS = {TRANSLATIONS[language]["default_question"] for language in ("en", "fr")} | {"Analyze the entire document"}


def utcnow():
    """Return the current UTC time as ISO 8601 text, to the nearest second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db_conn():
    """Open and return a SQLite connection to the jobs database.

    Rows are accessible by column name through sqlite3.Row. Wait up to
    20 seconds if the database is locked. The context commits or rolls back
    the transaction and always closes the connection afterward.
    """
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db():
    """Prepare the database and requeue interrupted jobs.

    Create DATA and the jobs table when needed, enable WAL journaling, and
    add the progress column to older databases. ``running`` rows become
    ``queued`` so the worker can resume them. Call this at startup before
    starting the worker.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    event("workspace_ready", status="available")
    with db_conn() as con:
        event("sqlite_initializing", status="connected")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
          pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, done_chunks INTEGER DEFAULT 0,
          error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          progress_percent INTEGER NOT NULL DEFAULT 0
        )""")
        columns = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        if "progress_percent" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0")
            event("sqlite_migration", column="progress_percent", status="applied")
        job_store.migrate(con)
        interrupted = con.execute("SELECT id,cancel_requested FROM jobs WHERE status='running' OR (status='queued' AND cancel_requested=1)").fetchall()
        con.execute("UPDATE jobs SET status='cancelled', stage='Cancelled', phase_code='cancelled' WHERE cancel_requested=1 AND status IN ('queued','running')")
        con.execute("UPDATE jobs SET status='queued', stage='Resuming after restart', queued_at=? WHERE status='running'", (utcnow(),))
        for row in interrupted:
            event("job_recovered", job_id=row["id"], status="cancelled" if row["cancel_requested"] else "queued")
    event("sqlite_ready", status="initialized")


def update_job(job_id, **values):
    """Store values and the timestamp for job_id.

    values maps column names selected by the caller to their new values.
    Update updated_at and force 100 percent when the status becomes
    ``done``. Do nothing when no values are supplied. An unknown ID creates
    no row. Return no value.
    """
    if not values:
        return
    if values.get("status") == "done":
        values["progress_percent"] = 100
    allowed = {"status", "stage", "pages", "total_chunks", "done_chunks", "error", "progress_percent",
               "phase_code", "phase_args", "error_code", "cancel_requested", "queued_at", "report_name"}
    if set(values) - allowed:
        raise ValueError("Unsupported job update field")
    values["updated_at"] = utcnow()
    parts = ", ".join(f"{k}=?" for k in values)
    with db_conn() as con:
        if values.get("status") == "done":
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["cancel_requested"]:
                raise Cancelled()
        con.execute(f"UPDATE jobs SET {parts} WHERE id=?", [*values.values(), job_id])
    if "status" in values:
        event("job_state", job_id=job_id, status=values["status"], stage=values.get("phase_code"))
    elif "progress_percent" in values:
        event("job_progress", job_id=job_id, stage=values.get("phase_code"),
              progress_percent=values["progress_percent"])


def report_available(job_id):
    """Cheaply tell the list whether a final report can be offered.

    Use lstat only: the jobs root and job directory must be real directories
    and the report a regular file, never a symlink. resolve_report() repeats
    the full path checks before serving it.
    """
    if not job_store.is_job_id(job_id):
        return False
    root = DATA / "jobs"
    try:
        return (stat.S_ISDIR(os.lstat(root).st_mode) and stat.S_ISDIR(os.lstat(root / job_id).st_mode)
                and stat.S_ISREG(os.lstat(root / job_id / "rapport_final.md").st_mode))
    except OSError:
        return False


def serialize_job(row):
    """Convert a SQLite row into a consistent API dictionary.

    row must contain at least the status and progress_percent fields.
    Present every ``done`` job at 100 percent, even if an older database
    still stores zero. This normalization does not rewrite the SQLite row.
    """
    public = {"id", "filename", "question", "status", "stage", "pages", "total_chunks", "done_chunks",
              "error", "created_at", "updated_at", "progress_percent", "phase_code", "phase_args",
              "error_code", "cancel_requested", "queued_at", "has_active_jobs", "report_name", "question_custom"}
    job = {key: row[key] for key in row.keys() if key in public}
    job["phase_args"] = json.loads(job.get("phase_args") or "{}")
    # The migration also initialized older completed jobs to 0 percent.
    if job["status"] == "done":
        job["progress_percent"] = 100
        job["report_available"] = report_available(job["id"])
    return job


def get_job(job_id):
    """Return the normalized job matching job_id, or None.

    The private configuration snapshot is not loaded: polling routes and the
    pipeline only need the job row. job_info adds the public configuration.
    """
    with db_conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return None if row is None else serialize_job(row)


def job_dir(job_id):
    """Build a job directory path without creating the directory.

    job_id must contain 32 lowercase hexadecimal characters. Otherwise,
    abort the request with HTTP 404. This validation prevents using an ID as
    an arbitrary path. It does not check whether the job or directory exists.
    """
    # IDs are generated by the application, never from the PDF filename.
    if not job_store.is_job_id(job_id):
        event("invalid_job_id", logging.WARNING, status="rejected")
        abort(404)
    directory = DATA / "jobs" / job_id
    # Also reject symlinked job roots, not only traversal in the ID.
    if directory.resolve() != DATA.resolve() / "jobs" / job_id:
        event("invalid_job_path", logging.WARNING, job_id=job_id, status="rejected")
        abort(404)
    return directory


def atomic_text(path, text):
    """Atomically replace a file with UTF-8 text through a temporary file.

    path is a path or Path object whose parent must already exist. Write to
    a neighboring file ending in .tmp first, then move it with os.replace so
    readers never see partial content. Return no value and propagate I/O
    errors.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        event("artifact_write_failed", logging.ERROR, artifact=path.name,
              job_id=current_job_id(), error_type=type(exc).__name__)
        raise


def atomic_json(path, data):
    """Atomically write data as UTF-8 JSON to path.

    data must be JSON serializable. Preserve Unicode characters, then
    delegate the write to atomic_text; the parent directory must already
    exist.
    """
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_system_prompt():
    """Read the system prompt file and strip surrounding whitespace.

    Capture the file for a new job; existing jobs use their private snapshots.
    Return a safe configuration error when it is missing or unreadable.
    """
    try:
        return SYSTEM_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise job_config.ConfigurationError("missing_prompt", "The system prompt is missing or unreadable.") from None


def runtime_defaults():
    """Return administrative defaults; snapshots isolate existing jobs from changes."""
    return {
        "chunk_tokens": CHUNK_TOKENS, "context_tokens": CONTEXT_TOKENS,
        "analysis_tokens": ANALYSIS_TOKENS, "merge_tokens": MERGE_TOKENS,
        "final_tokens": FINAL_TOKENS, "report_language": OUTPUT_LANGUAGE,
        "temperature": 0.2, "top_p": None, "top_k": None, "min_p": None,
        "thinking_section": "inherit", "thinking_merge": "inherit", "thinking_final": "inherit",
        "thinking_adapter": THINKING_ADAPTER, "initial_thinking": INITIAL_THINKING,
        "apply_template": APPLY_TEMPLATE_SUPPORTED, "usage_supported": USAGE_SUPPORTED,
        "request_timeout": REQUEST_TIMEOUT, "step_timeout_seconds": STEP_TIMEOUT,
        "max_calls": MAX_MODEL_CALLS, "max_attempts": MAX_JOB_ATTEMPTS,
        "extraction": {"version": 3, "enabled": OCR_ENABLED, "languages": OCR_LANGUAGES,
                       "dpi": OCR_DPI, "max_pixels": OCR_MAX_PIXELS, "timeout": OCR_TIMEOUT,
                       "max_pages": MAX_PAGES},
    }


def new_snapshot(options, capabilities=None):
    """Capture actual private prompt text and templates, not only their fingerprints."""
    config = job_config.resolve(runtime_defaults(), options, capabilities)
    config.update(system_prompt=read_system_prompt(), templates=dict(prompts.TEMPLATES),
                  prompt_version=prompts.PROMPT_VERSION)
    if not config["system_prompt"]:
        raise job_config.ConfigurationError("missing_prompt", "The system prompt is empty.")
    return config


class ModelClient(BackendClient):
    """Bind the reusable backend adapter to this application's deployment settings."""

    def __init__(self, config=None):
        settings = config if config is not None else runtime_defaults()
        prompt = settings.get("system_prompt")
        super().__init__(LLAMA_BASE, KEY, settings,
                         read_system_prompt() if prompt is None else prompt, LIVE_PREVIEW_CHARS)

    def inspect(self):
        """Share each job's read-only inspection with form validation and diagnostics."""
        value = super().inspect()
        remember_capabilities(value)
        return value


def output_language_instruction():
    """Build the language instruction from OUTPUT_LANGUAGE.

    In ``auto`` mode, request the language used by the user's question.
    Otherwise, explicitly request the configured language.
    """
    if OUTPUT_LANGUAGE.lower() == "auto":
        return "Write in the same language as the user's request."
    return f"Write in {OUTPUT_LANGUAGE}."


def analyze_job(job_id):
    """Run the versioned pipeline while preserving the public Python entry point."""
    return pipeline.run(job_id, sys.modules[__name__])


CANCEL_RECHECK_SECONDS = 1.0


class _CancelSignal:
    """In-process cancellation flag for the running job; SQLite stays authoritative."""

    def __init__(self):
        self.event = threading.Event()
        self.checked_at = float("-inf")


# Registered by the worker for its running job only; cancel_job sets the event.
_cancel_signals: dict[str, _CancelSignal] = {}


def read_cancellation(job_id):
    """Read the persisted cancellation request; a deleted job counts as cancelled."""
    with db_conn() as con:
        row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    return not row or bool(row["cancel_requested"])


def cancellation_requested(job_id):
    """Answer frequent cooperative checks without opening SQLite each time.

    The model client checks once per streamed token. A request through
    cancel_job sets the running job's event immediately; SQLite is reread at
    most once per CANCEL_RECHECK_SECONDS so any other recorded request is
    still honored. Jobs not run by the worker always read SQLite.
    """
    signal = _cancel_signals.get(job_id)
    if signal is None:
        return read_cancellation(job_id)
    if not signal.event.is_set():
        now = time.monotonic()
        if now - signal.checked_at >= CANCEL_RECHECK_SECONDS:
            signal.checked_at = now
            if read_cancellation(job_id):
                signal.event.set()
    return signal.event.is_set()


def fail_job(job_id, exc):
    """Record a failed job with a translatable code and a message safe to display.

    Only ProcessingError and ConfigurationError carry public text; any other
    exception is logged without its message and shown as a generic failure.
    """
    failure("job_failed", exc, job_id=job_id)
    if isinstance(exc, (ProcessingError, job_config.ConfigurationError)):
        code, message = exc.code, str(exc)
    else:
        code, message = "analysis_failed", "Analysis failed. Check the PDF, configuration and local diagnostics."
    update_job(job_id, status="failed", stage="Error", phase_code="failed", error_code=code, error=message)


def run_claimed_job(job_id):
    """Run a job already marked ``running`` and record how it ended."""
    _cancel_signals[job_id] = _CancelSignal()
    event("job_state", job_id=job_id, status="running", stage="starting")
    with job_context(job_id):
        try:
            analyze_job(job_id)
        except Cancelled:
            update_job(job_id, status="cancelled", stage="Cancelled", phase_code="cancelled", error="", error_code="cancelled")
        except Exception as exc:
            # Keep the last preview to help diagnose the error.
            fail_job(job_id, exc)
        finally:
            _cancel_signals.pop(job_id, None)


def worker_loop():
    """Process jobs forever in creation order.

    Claim the next job by changing it from ``queued`` to ``running``, then
    run it through run_claimed_job, keeping produced files. When idle, wait
    for a worker_wakeup signal or a six-second timeout. This loop belongs to
    the application's only worker thread.
    """
    while True:
        try:
            with db_conn() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
                if row:
                    con.execute("UPDATE jobs SET status='running', stage='Starting', updated_at=? WHERE id=?", (utcnow(), row["id"]))
            if row:
                run_claimed_job(row["id"])
                continue
        except Exception as exc:
            failure("worker_database_error", exc)
        worker_wakeup.wait(timeout=6)
        worker_wakeup.clear()


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    """Return JSON with HTTP 413 for an oversized request body.

    The _ parameter is Flask's exception and is unused here.
    """
    event("upload_rejected", logging.WARNING, error_code="upload_limit")
    return jsonify(error=f"PDF too large (limit: {MAX_UPLOAD_MB} MiB)", error_code="upload_limit"), 413


@app.get("/")
def index():
    """Serve the HTML page and its PDF size limit."""
    return render_template("index.html", limit=MAX_UPLOAD_MB, translations=TRANSLATIONS)


@app.get("/api/health")
def health():
    """Return HTTP 200 without checking the model or worker."""
    return jsonify(status="ok")


@app.get("/api/jobs")
def list_jobs():
    """Return the 100 most recent jobs and activity across the whole queue.

    has_active_jobs includes older jobs outside the visible list so the
    browser keeps polling while any job is queued or running.
    """
    with db_conn() as con:
        rows = con.execute("""SELECT *,
            EXISTS(SELECT 1 FROM jobs WHERE status IN ('queued', 'running'))
                AS has_active_jobs
            FROM jobs ORDER BY created_at DESC, id DESC LIMIT 100""").fetchall()
        # Reuse the fingerprint stored with each snapshot instead of rehashing it on every poll.
        configs = {row["job_id"]: (json.loads(row["snapshot"]), row["fingerprint"]) for row in con.execute(
            "SELECT job_id, snapshot, fingerprint FROM job_configs WHERE job_id IN "
            "(SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT 100)")}
    has_active_jobs = bool(rows and rows[0]["has_active_jobs"])
    jobs = [serialize_job(row) for row in rows]
    for job in jobs:
        job.pop("has_active_jobs")
        if job["id"] in configs:
            job["configuration"] = job_config.public_config(*configs[job["id"]])
    return jsonify(jobs=jobs, has_active_jobs=has_active_jobs)


@app.get("/api/jobs/<job_id>")
def job_info(job_id):
    """Return job_id and its verified public configuration, or HTTP 404."""
    job = get_job(job_id)
    if not job:
        abort(404)
    config = job_store.load_config(db_conn, job_id)
    if config:
        job["configuration"] = job_config.public_config(config)
    return jsonify(job)


def delete_history_job(job_id):
    """Commit history removal before touching files; serialize against retry/claim."""
    if not job_store.is_job_id(job_id):
        return {"id": job_id, "deleted": False, "error_code": "invalid_job_id"}, 400
    try:
        with db_conn() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return {"id": job_id, "deleted": False, "error_code": "job_not_found"}, 404
            # A completed status can precede the worker's final metrics writes.
            finishing = con.execute(
                "SELECT 1 FROM job_runs WHERE job_id=? AND status='running' LIMIT 1", (job_id,)
            ).fetchone()
            if row["status"] not in ("done", "failed", "cancelled") or finishing:
                return {"id": job_id, "deleted": False, "error_code": "delete_active"}, 409
            con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    except sqlite3.Error as exc:
        failure("history_delete_failed", exc, job_id=job_id)
        return {"id": job_id, "deleted": False, "error_code": "delete_database"}, 500
    warning = job_store.remove_private_files(DATA, job_id)
    event("history_deleted", job_id=job_id, status="files_retained" if warning else "deleted")
    return {"id": job_id, "deleted": True, "warning_code": warning}, 200


@app.post("/api/jobs/<job_id>/delete")
def delete_job(job_id):
    """Delete one terminal analysis using only its server-generated ID."""
    result, status = delete_history_job(job_id)
    return jsonify(result), status


@app.post("/api/jobs/delete")
def delete_jobs():
    """Return an explicit outcome per unique ID, including partial failures."""
    if request.content_length and request.content_length > 8192:
        return jsonify(error_code="invalid_job_ids"), 400
    payload = request.get_json(silent=True)
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or not all(job_store.is_job_id(value) for value in ids)):
        return jsonify(error_code="invalid_job_ids"), 400
    results = [delete_history_job(job_id)[0] for job_id in dict.fromkeys(ids)]
    return jsonify(results=results), 200 if all(item["deleted"] for item in results) else 207


@app.post("/api/jobs")
def create_job():
    """Store the uploaded PDF and create a queued job.

    Read the multipart ``file`` and ``question`` fields. Use the default
    question when absent; validate its content and length, the filename
    extension, and the PDF header. The worker performs actual analysis and
    page-limit validation.

    Save source.pdf, insert the job, and wake the worker. Return HTTP 201
    with the ID and status, or HTTP 400 for an invalid form. Remove the file
    if insertion fails.
    """
    options = job_config.parse_options(request.form.get("options"))
    config = new_snapshot(options, cached_capabilities())
    uploaded = request.files.get("file")
    question = request.form.get("question") or "Analyze the entire document"
    if not uploaded or not uploaded.filename:
        return jsonify(error="Select a PDF file", error_code="invalid_pdf"), 400
    if not question.strip() or len(question) > MAX_QUESTION_CHARS:
        return jsonify(error=f"Instructions required (maximum {MAX_QUESTION_CHARS} characters)", error_code="invalid_question"), 400
    filename = uploaded.filename.replace("\\", "/").split("/")[-1][:120]
    if not filename.lower().endswith(".pdf"):
        return jsonify(error="Only .pdf files are accepted", error_code="invalid_pdf"), 400
    if uploaded.stream.read(5) != b"%PDF-":
        return jsonify(error="The file does not appear to be a valid PDF", error_code="invalid_pdf"), 400
    uploaded.stream.seek(0)
    job_id = uuid.uuid4().hex
    directory = job_dir(job_id)
    directory.mkdir(parents=True, mode=0o700)
    try:
        uploaded.save(directory / "source.pdf")
        if (directory / "source.pdf").stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
            raise RequestEntityTooLarge()
        config["source_hash"] = job_store.file_hash(directory / "source.pdf")
        with db_conn() as con:
            con.execute("""INSERT INTO jobs (id, filename, question, status, stage, created_at, updated_at, question_custom)
              VALUES (?, ?, ?, 'queued', 'Queued', ?, ?, ?)""",
                        (job_id, filename, question, utcnow(), utcnow(), int(question.strip() not in DEFAULT_QUESTIONS)))
            con.execute("UPDATE jobs SET queued_at=? WHERE id=?", (utcnow(), job_id))
            job_store.save_config(con, job_id, config)
    except Exception:
        (directory / "source.pdf").unlink(missing_ok=True)
        directory.rmdir()
        raise
    worker_wakeup.set()
    event("job_created", job_id=job_id, filename=filename, bytes=(directory / "source.pdf").stat().st_size,
          profile=config["profile"], custom_prompt=question.strip() not in DEFAULT_QUESTIONS,
          chunk_tokens=config["chunk_tokens"], analysis_tokens=config["analysis_tokens"],
          merge_tokens=config["merge_tokens"], final_tokens=config["final_tokens"],
          report_language=config["report_language"])
    return jsonify(id=job_id, status="queued", configuration=job_config.public_config(config)), 201


@app.get("/api/jobs/<job_id>/live")
def job_live(job_id):
    """Return the live preview for job_id, or HTTP 404.

    Read the preview JSON without calling the model. Return an empty preview
    when live output is disabled, missing, or unreadable. A completed or
    failed job is never reported as still generating.
    """
    job = get_job(job_id)
    if not job:
        abort(404)
    path = job_dir(job_id) / "live_preview.json"
    if not LIVE_OUTPUT_ENABLED or not path.exists():
        return jsonify(phase=job["stage"], content="", reasoning="",
                       active=False, enabled=LIVE_OUTPUT_ENABLED)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify(phase=job["stage"], content="", reasoning="", active=False)
    # The last file may have been written before the job completed or failed.
    payload["active"] = bool(payload.get("active") and job["status"] == "running")
    if payload["active"] and payload.get("started_at"):
        try:
            payload["elapsed_seconds"] = round(max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(payload["started_at"])).total_seconds()), 1)
        except (ValueError, TypeError):
            pass
    payload["enabled"] = True
    if request.args.get("include_reasoning") != "true":
        payload["reasoning"] = ""
    payload["reasoning_available"] = bool(payload.get("reasoning_chars"))
    payload["phase_code"] = job["phase_code"]
    payload["phase_args"] = job["phase_args"]
    return jsonify(payload)


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    """Requeue a failed job and wake the worker.

    Keep files and progress for resumption. Clear the error and return
    HTTP 200; return 404 when the job is absent or 409 when its status is not
    ``failed``.
    """
    job = get_job(job_id)
    if not job:
        abort(404)
    if request.data or request.form:
        return jsonify(error="Retry uses the original snapshot. Create a new job to change options.", error_code="immutable_config"), 400
    with db_conn() as con:
        con.execute("BEGIN IMMEDIATE")
        attempts = con.execute("SELECT COUNT(*) FROM job_runs WHERE job_id=?", (job_id,)).fetchone()[0]
        config_row = con.execute("SELECT snapshot FROM job_configs WHERE job_id=?", (job_id,)).fetchone()
        maximum = json.loads(config_row["snapshot"])["max_attempts"] if config_row else runtime_defaults()["max_attempts"]
        if attempts >= maximum:
            return jsonify(error="The retry limit was reached. Create a new analysis.", error_code="retry_limit"), 409
        changed = con.execute("""UPDATE jobs SET status='queued', error='', error_code='', stage='Retrying',
            phase_code='queued', cancel_requested=0, queued_at=?, updated_at=?
            WHERE id=? AND status IN ('failed','cancelled')""", (utcnow(), utcnow(), job_id)).rowcount
    if not changed:
        return jsonify(error="Only failed or cancelled jobs can be retried", error_code="retry_conflict"), 409
    worker_wakeup.set()
    legacy = job_store.load_config(db_conn, job_id) is None
    event("job_retry", job_id=job_id, attempt=attempts + 1, status="queued", legacy=legacy)
    return jsonify(status="queued", warnings=["legacy_recomputed"] if legacy else [])



@app.get("/api/jobs/<job_id>/report")
def report(job_id):
    """Stream the original Markdown with a descriptive attachment filename."""
    try:
        job, path = resolve_report(job_id)
        name = ensure_report_name(job, path)
        response = send_file(path, mimetype="text/markdown", as_attachment=True, download_name=name)
    except OSError:
        event("report_missing", logging.WARNING, job_id=job_id, status="unavailable")
        abort(404)
    event("report_download", job_id=job_id, filename=name, bytes=response.content_length)
    # Names are portable ASCII; include the RFC 5987 form for UTF-8-aware clients too.
    response.headers["Content-Disposition"] += "; filename*=UTF-8''" + name
    response.headers["Content-Type"] = "text/markdown; charset=utf-8"
    return response


def resolve_report(job_id):
    """Authorize the only publicly available artifact; never accept a filename."""
    directory = job_dir(job_id)
    job = get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        abort(409)
    path = directory / "rapport_final.md"
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        event("report_missing", logging.WARNING, job_id=job_id, status="unavailable")
        abort(404)
    return job, path


def ensure_report_name(job, path):
    """Calculate old report names lazily, without loading a whole file or history."""
    if job.get("report_name"):
        return job["report_name"]
    with path.open(encoding="utf-8", errors="replace") as stream:
        name = reports.report_filename(stream.read(reports.TITLE_SCAN_CHARS), job["filename"])
    with db_conn() as con:
        con.execute("UPDATE jobs SET report_name=? WHERE id=? AND report_name IS NULL", (name, job["id"]))
    return name


@app.get("/api/jobs/<job_id>/report/view")
def view_report(job_id):
    """Render bounded Markdown on the server; viewing never schedules model work."""
    language = request.args.get("lang", "en")
    if language not in ("en", "fr"):
        language = "en"

    def translate(key):
        return TRANSLATIONS[language].get(key, key)

    try:
        job, path = resolve_report(job_id)
        name = ensure_report_name(job, path)
        # Bounded read also handles a file growing between stat and read.
        with path.open("rb") as stream:
            content = stream.read(reports.PREVIEW_MAX_BYTES + 1)
        if len(content) > reports.PREVIEW_MAX_BYTES:
            html, message, status = "", "Preview is limited to 1 MiB. Download the original Markdown.", 413
        else:
            html = reports.render_markdown(content.decode("utf-8")) if content.strip() else ""
            message, status = ("" if html else "No content is available."), 200
    except (OSError, UnicodeError):
        event("report_unreadable", logging.WARNING, job_id=job_id)
        name, html, message, status = translate("Markdown report"), "", "Report not found or unreadable.", 404
    except (NotFound, Conflict) as exc:
        name, html, message, status = translate("Markdown report"), "", "Report not found or not ready.", exc.code
    event("report_view", job_id=job_id, status=status)
    response = make_response(render_template("report.html", report_name=name, content=html, message=message,
        job_id=job_id, available=status in (200, 413), language=language, t=translate), status)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'none'; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


_capability_cache: dict[str, Any] = {"value": None, "expires": 0}
_capability_lock = threading.Lock()
CAPABILITY_CACHE_SECONDS = 30


def cached_capabilities():
    """Read an advisory cache without initiating network traffic during form submission."""
    if time.monotonic() < _capability_cache["expires"]:
        return _capability_cache["value"]
    return None


def remember_capabilities(value):
    """Publish a sanitized inspection from diagnostics or a job start for a short period."""
    # Readers check the expiry first, so publish the value before extending it.
    _capability_cache["value"] = value
    _capability_cache["expires"] = time.monotonic() + CAPABILITY_CACHE_SECONDS


@app.before_request
def begin_request():
    """Measure request latency and reject browser writes originating on another site."""
    g.started = time.monotonic()
    origin = request.headers.get("Origin")
    if request.method == "POST" and origin:
        if urlsplit(origin).netloc != request.host or urlsplit(origin).scheme not in ("http", "https"):
            return jsonify(error="Cross-origin writes are not allowed.", error_code="origin_rejected"), 403


@app.after_request
def finish_request(response):
    """Keep private job responses uncached and record download latency without content."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    if request.endpoint in ("report", "view_report"):
        event("report_response", job_id=(request.view_args or {}).get("job_id"), status=response.status_code,
              duration_ms=round((time.monotonic() - g.started) * 1000, 1), bytes=response.content_length)
    if response.status_code >= 400 and request.method == "POST":
        event("request_rejected", logging.WARNING, endpoint=request.endpoint, status=response.status_code)
    return response


@app.errorhandler(job_config.ConfigurationError)
def invalid_configuration(exc):
    """Return stable validation codes for translated browser errors."""
    event("configuration_rejected", logging.WARNING, error_code=exc.code)
    return jsonify(error=str(exc), error_code=exc.code), 400


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(500)
def api_error(exc):
    """Never expose exception internals, local paths or backend response bodies."""
    code = getattr(exc, "code", 500)
    messages = {400: "Invalid request.", 404: "Job or file not found.",
                409: "The result is not ready.", 500: "An internal error occurred."}
    return jsonify(error=messages.get(code, "Request failed."), error_code=f"http_{code}"), code


@app.get("/api/profiles")
def profile_info():
    """Describe profiles without running a model or altering server settings."""
    return jsonify(profiles=profiles.catalog(),
                   defaults=job_config.public_config(job_config.resolve(runtime_defaults())),
                   capabilities=cached_capabilities(),
                   thinking_supported=runtime_defaults()["thinking_adapter"] != "unsupported")


@app.post("/api/config/preview")
def config_preview():
    """Resolve a small JSON request; the server repeats validation on job creation."""
    if request.content_length and request.content_length > 8192:
        raise job_config.ConfigurationError("invalid_options", "Options are too large.")
    options = request.get_json()
    if not isinstance(options, dict):
        raise job_config.ConfigurationError("invalid_options", "Options must be a JSON object.")
    config = job_config.resolve(runtime_defaults(), options, cached_capabilities())
    return jsonify(configuration=job_config.public_config(config),
                   capabilities=cached_capabilities(), validation="preliminary")


@app.get("/api/diagnostics")
def diagnostics():
    """Expose only read-only, sanitized properties and this process's own resources."""
    with _capability_lock:
        value = cached_capabilities()
        if value is None:
            # Inspection needs no private system prompt and launches no completion.
            client = BackendClient(LLAMA_BASE, KEY, runtime_defaults(), "")
            try:
                value = client.inspect()
                event("backend_checked", status=value.get("status", "unknown"), source="diagnostics")
            finally:
                client.session.close()
            remember_capabilities(value)
    process_rss_kib = None
    try:
        matched = re.search(r"^VmRSS:\s+(\d+)", Path("/proc/self/status").read_text(encoding="utf-8"), re.M)
        if matched:
            process_rss_kib = int(matched[1])
    except OSError:
        pass
    return jsonify(backend=value, application={"context_limit": CONTEXT_TOKENS, "concurrency": 1,
                   "process_rss_kib": process_rss_kib, "resource_scope": "processor_only"},
                   engine_settings="read_only", engine_memory=None, engine_swap_rate=None)


@app.get("/api/jobs/<job_id>/metrics")
def job_metrics(job_id):
    """Serve bounded measurements; no prompts, documents or reasoning are included."""
    if not get_job(job_id):
        abort(404)
    try:
        limit = int(request.args.get("limit", "200"))
        before = int(request.args["before"]) if "before" in request.args else None
        if not 1 <= limit <= 500 or (before is not None and not 0 < before < 2**63):
            raise ValueError()
    except ValueError:
        abort(400)
    return jsonify(job_store.metrics(db_conn, job_id, limit, before))


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    """Request cancellation idempotently; active OCR stops at its next safe boundary."""
    with db_conn() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT status,cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            abort(404)
        if row["status"] == "cancelled":
            return jsonify(status="cancelled"), 200
        if row["status"] not in ("queued", "running"):
            return jsonify(error="This job cannot be cancelled.", error_code="cancel_conflict"), 409
        if row["cancel_requested"]:
            return jsonify(status=row["status"], cancel_requested=True), 202
        queued = row["status"] == "queued"
        con.execute("UPDATE jobs SET cancel_requested=1, status=?, phase_code=?, stage=?, updated_at=? WHERE id=?",
                    ("cancelled" if queued else "running", "cancelled" if queued else "cancelling",
                     "Cancelled" if queued else "Cancellation requested", utcnow(), job_id))
    # The request is committed; let the running job see it without waiting for its next SQLite read.
    signal = _cancel_signals.get(job_id)
    if signal is not None:
        signal.event.set()
    worker_wakeup.set()
    event("job_cancel_requested", job_id=job_id, status="cancelled" if queued else "cancelling")
    return jsonify(status="cancelled" if queued else "running", cancel_requested=True), 202


if __name__ == "__main__":
    event("processor_initializing", status="starting")
    try:
        job_config.resolve(runtime_defaults())
    except job_config.ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    init_db()
    threading.Thread(target=worker_loop, name="pdf-worker", daemon=True).start()
    log.info("processor_started port=%s", APP_PORT)
    serve(app, host="0.0.0.0", port=APP_PORT, threads=4)
