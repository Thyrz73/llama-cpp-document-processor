"""Web entry point: deployment settings, HTTP routes and startup.

This module only wires things together and speaks HTTP; the work happens in
the document_processor package:

- jobs.JobStore keeps jobs in SQLite and their files under DATA_DIR;
- worker.Worker runs queued jobs one at a time in a background thread;
- pipeline.run() analyzes one PDF (text extraction, section notes, merges,
  final report) with the llama.cpp server through model_client.

Settings are read from the environment when this module is imported.
Running ``python app.py`` prepares the database, starts the worker and serves
the API with Waitress; importing the module starts neither.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, abort, g, jsonify, make_response, render_template, request, send_file
from waitress import serve
from werkzeug.exceptions import Conflict, NotFound, RequestEntityTooLarge

from document_processor import config as job_config
from document_processor import environment, pipeline, profiles, prompts, reports
from document_processor.database import SchemaVersionError
from document_processor.errors import ModelError
from document_processor.files import file_hash
from document_processor.jobs import PREVIEW_FILE, REPORT_FILE, SOURCE_FILE, InvalidJobPath, JobStore, is_job_id
from document_processor.model_client import ModelClient as BackendClient
from document_processor.observability import configure_logging, event, failure
from document_processor.worker import Worker

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

configure_logging()
log = logging.getLogger("document-processor")
# All job records and files live under DATA; see document_processor/jobs.py.
store = JobStore(DATA)


class ProcessorFlask(Flask):
    def log_exception(self, exc_info):
        """Prevent Flask's default traceback logger from leaking exception bodies."""
        failure("request_failed", exc_info[1], endpoint=request.endpoint)


app = ProcessorFlask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024 + 65536
TRANSLATIONS = json.loads((Path(__file__).parent / "static" / "translations.json").read_text(encoding="utf-8"))
# Used when a request omits the question; older interfaces also sent it.
FALLBACK_QUESTION = "Analyze the entire document"
DEFAULT_QUESTIONS = {TRANSLATIONS[language]["default_question"] for language in ("en", "fr")} | {FALLBACK_QUESTION}


def is_default_question(question):
    """Tell whether a request is one of the interface's default instructions."""
    return question.strip() in DEFAULT_QUESTIONS


def public_job(job):
    """Apply the default-instructions rule to jobs saved before it was recorded.

    Their question_custom stays null unless the request is a known default,
    so the browser never needs its own copy of the default questions.
    """
    if job.get("question_custom") is None and job.get("question") and is_default_question(job["question"]):
        job["question_custom"] = 0
    return job


# Job configuration and the llama.cpp client -----------------------------------


def read_system_prompt():
    """Read the system prompt file and strip surrounding whitespace.

    New jobs save this text in their snapshot; existing jobs keep their copy.
    Raise ConfigurationError when the file is missing or unreadable.
    """
    try:
        return SYSTEM_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise job_config.ConfigurationError("missing_prompt", "The system prompt is missing or unreadable.") from None


def runtime_defaults():
    """Return the deployment's default job configuration, read from the environment.

    New jobs start from it; existing jobs keep the configuration of their snapshot.
    """
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
    """Resolve options into the configuration snapshot of a new job.

    The snapshot includes the current system prompt and task templates, so a
    retry uses the same text even after these files change. Raise
    ConfigurationError for invalid options or an empty system prompt.
    """
    config = job_config.resolve(runtime_defaults(), options, capabilities)
    config.update(system_prompt=read_system_prompt(), templates=dict(prompts.TEMPLATES),
                  prompt_version=prompts.PROMPT_VERSION)
    if not config["system_prompt"]:
        raise job_config.ConfigurationError("missing_prompt", "The system prompt is empty.")
    return config


_capability_cache: dict[str, Any] = {"value": None, "expires": 0}
_capability_lock = threading.Lock()
CAPABILITY_CACHE_SECONDS = 30
CAPABILITY_FAILURE_CACHE_SECONDS = 5


def capability_backend(client=None):
    """Identify the configured backend privately, without retaining another credential copy."""
    base = LLAMA_BASE if client is None else client.base_url
    authorization = (f"Bearer {KEY}" if KEY else "") if client is None else client.session.headers.get("Authorization", "")
    return base.rstrip("/"), hashlib.sha256(authorization.encode("utf-8")).digest()


def cached_capabilities(client=None):
    """Return an unexpired inspection for this backend, without network I/O.

    Complete successes live for 30 seconds; failures and unknown contexts
    live for 5 seconds. Reads never extend the original expiration.
    """
    cached = _capability_cache
    if cached.get("backend") == capability_backend(client) and time.monotonic() < cached["expires"]:
        return copy.deepcopy(cached["value"])
    return None


def remember_capabilities(value, client=None):
    """Publish one complete cache entry; callers serialize inspections with the lock."""
    global _capability_cache
    context = value.get("context_tokens")
    healthy = value.get("status") == "available" and type(context) is int and context > 0
    ttl = CAPABILITY_CACHE_SECONDS if healthy else CAPABILITY_FAILURE_CACHE_SECONDS
    _capability_cache = {"value": copy.deepcopy(value), "expires": time.monotonic() + ttl,
                         "backend": capability_backend(client),
                         "model_name": client.model_name if client is not None else None}


def invalidate_capabilities(client):
    """Discard observations after a backend failure, leaving cancellation independent."""
    global _capability_cache
    if _capability_cache.get("backend") == capability_backend(client):
        _capability_cache = {"value": None, "expires": 0}


class ModelClient(BackendClient):
    """Bind the reusable backend adapter to this application's deployment settings."""

    def __init__(self, config=None):
        settings = config if config is not None else runtime_defaults()
        prompt = settings.get("system_prompt")
        super().__init__(LLAMA_BASE, KEY, settings,
                         read_system_prompt() if prompt is None else prompt, LIVE_PREVIEW_CHARS)

    def inspect(self, *, cached=None):
        """Read capabilities for display, allowing the shared TTL cache."""
        return self._inspect_capabilities(for_identity=False)

    def inspect_identity(self):
        """Verify a new attempt's identity without using UI cache entries."""
        return self._inspect_capabilities(for_identity=True)

    def _inspect_capabilities(self, *, for_identity):
        """Serialize observations and publish fresh identity checks to the UI cache."""
        self.check_cancelled()
        while not _capability_lock.acquire(timeout=0.2):
            self.check_cancelled()
        try:
            self.check_cancelled()
            previous = None if for_identity else cached_capabilities(self)
            if previous is not None and previous.get("status") == "unavailable":
                self.capabilities = previous
                self.model_name = None
                return previous
            model_name = _capability_cache.get("model_name") if previous is not None else None
            if for_identity:
                try:
                    value = super().inspect_identity()
                except ModelError:
                    remember_capabilities(self.capabilities, self)
                    raise
            else:
                value = super().inspect(cached=(model_name, previous) if previous is not None else None)
            if previous is None or self.model_name != model_name or value != previous:
                remember_capabilities(value, self)
            return value
        finally:
            _capability_lock.release()

    def json_request(self, *args, **kwargs):
        """Invalidate cached properties on failed or malformed backend requests."""
        try:
            return super().json_request(*args, **kwargs)
        except ModelError:
            invalidate_capabilities(self)
            raise

    def generate(self, *args, **kwargs):
        """Do not retain capabilities after a generation fails at the backend."""
        try:
            return super().generate(*args, **kwargs)
        except ModelError:
            invalidate_capabilities(self)
            raise

    def tokenize(self, text):
        """Discard capabilities if tokenization fails before generation starts."""
        try:
            return super().tokenize(text)
        except ModelError:
            invalidate_capabilities(self)
            raise


# Background analysis ----------------------------------------------------------


def analyze_job(job_id, cancellation_requested=None):
    """Run the analysis pipeline for job_id with this deployment's services.

    The worker passes its own cancellation check; without one, every check
    reads SQLite.
    """
    pipeline.run(job_id, pipeline.Runtime(
        store=store, new_snapshot=new_snapshot, model_client=ModelClient,
        cancellation_requested=cancellation_requested or store.read_cancellation,
        live_output=LIVE_OUTPUT_ENABLED, preview_chars=LIVE_PREVIEW_CHARS))


# The single background worker; routes wake it when the queue changes.
worker = Worker(store, analyze_job)


# HTTP routes ------------------------------------------------------------------


def job_directory(job_id):
    """Return the job directory, or answer HTTP 404 for an invalid ID or path."""
    try:
        return store.job_dir(job_id)
    except InvalidJobPath:
        abort(404)


@app.get("/")
def index():
    """Serve the interface page with its upload limit and translations."""
    return render_template("index.html", limit=MAX_UPLOAD_MB, translations=TRANSLATIONS)


@app.get("/api/health")
def health():
    """Return HTTP 200 without checking the model or worker."""
    return jsonify(status="ok")


@app.get("/api/profiles")
def profile_info():
    """Return the profiles, the default job configuration and the cached backend capabilities."""
    capabilities = cached_capabilities()
    defaults = job_config.resolve(runtime_defaults())
    context = (capabilities or {}).get("context_tokens")
    if type(context) is int and context > 0:
        defaults["warnings"].remove("engine_context_unknown")
    return jsonify(profiles=profiles.catalog(),
                   defaults=job_config.public_config(defaults),
                   capabilities=capabilities,
                   thinking_supported=runtime_defaults()["thinking_adapter"] != "unsupported")


@app.post("/api/config/preview")
def config_preview():
    """Resolve job options sent as JSON for the settings preview; job creation checks them again."""
    if request.content_length and request.content_length > 8192:
        raise job_config.ConfigurationError("invalid_options", "Options are too large.")
    options = request.get_json()
    if not isinstance(options, dict):
        raise job_config.ConfigurationError("invalid_options", "Options must be a JSON object.")
    capabilities = cached_capabilities()
    config = job_config.resolve(runtime_defaults(), options, capabilities)
    return jsonify(configuration=job_config.public_config(config),
                   capabilities=capabilities, validation="preliminary")


@app.get("/api/diagnostics")
def diagnostics():
    """Return the backend inspection, cached or made now, and this process's memory use.

    The inspection is sanitized and never asks the model to generate anything.
    """
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
            remember_capabilities(value, client)
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


@app.get("/api/jobs")
def list_jobs():
    """Return the 100 most recent jobs and activity across the whole queue.

    has_active_jobs includes older jobs outside the visible list so the
    browser keeps polling while any job is queued or running.
    """
    jobs, has_active_jobs = store.list_jobs(100)
    return jsonify(jobs=[public_job(job) for job in jobs], has_active_jobs=has_active_jobs)


@app.get("/api/jobs/<job_id>")
def job_info(job_id):
    """Return job_id and its verified public configuration, or HTTP 404."""
    job = store.get_job(job_id)
    if not job:
        abort(404)
    config = store.load_config(job_id)
    if config:
        job["configuration"] = job_config.public_config(config)
    return jsonify(public_job(job))


def delete_history_job(job_id):
    """Delete one finished job and return (result dictionary, HTTP status).

    The database record is deleted first and the files afterwards, so a file
    cleanup problem only adds a warning_code to a successful result.
    """
    if not is_job_id(job_id):
        return {"id": job_id, "deleted": False, "error_code": "invalid_job_id"}, 400
    try:
        error_code = store.delete_job(job_id)
    except sqlite3.Error as exc:
        failure("history_delete_failed", exc, job_id=job_id)
        return {"id": job_id, "deleted": False, "error_code": "delete_database"}, 500
    if error_code:
        return {"id": job_id, "deleted": False, "error_code": error_code}, 404 if error_code == "job_not_found" else 409
    warning = store.remove_files(job_id)
    event("history_deleted", job_id=job_id, status="files_retained" if warning else "deleted")
    return {"id": job_id, "deleted": True, "warning_code": warning}, 200


@app.post("/api/jobs/<job_id>/delete")
def delete_job(job_id):
    """Delete one terminal analysis using only its server-generated ID."""
    result, status = delete_history_job(job_id)
    return jsonify(result), status


@app.post("/api/jobs/delete")
def delete_jobs():
    """Delete up to 100 finished jobs by ID; return one result per unique ID, with HTTP 207 if any fails."""
    if request.content_length and request.content_length > 8192:
        return jsonify(error_code="invalid_job_ids"), 400
    payload = request.get_json(silent=True)
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or not all(is_job_id(value) for value in ids)):
        return jsonify(error_code="invalid_job_ids"), 400
    results = [delete_history_job(job_id)[0] for job_id in dict.fromkeys(ids)]
    return jsonify(results=results), 200 if all(item["deleted"] for item in results) else 207


@app.post("/api/jobs")
def create_job():
    """Save an uploaded PDF and queue a job for it.

    Read the multipart fields "file", "question" (the default instructions
    when empty) and "options". Resolve the options first, then check the
    question length, the .pdf extension and the PDF header; the worker checks
    the page count. Return HTTP 201 with the ID, status and public
    configuration, 400 for an invalid request or 413 for an oversized file.
    The saved PDF is removed if the job cannot be created.
    """
    options = job_config.parse_options(request.form.get("options"))
    config = new_snapshot(options, cached_capabilities())
    uploaded = request.files.get("file")
    question = request.form.get("question") or FALLBACK_QUESTION
    if not uploaded or not uploaded.filename:
        return jsonify(error="Select a PDF file", error_code="invalid_pdf"), 400
    if not question.strip() or len(question) > MAX_QUESTION_CHARS:
        return jsonify(error=f"Instructions required (maximum {MAX_QUESTION_CHARS} characters)", error_code="invalid_question"), 400
    config["effective_report_language"] = prompts.resolve_report_language(config["report_language"], question)
    filename = uploaded.filename.replace("\\", "/").split("/")[-1][:120]
    if not filename.lower().endswith(".pdf"):
        return jsonify(error="Only .pdf files are accepted", error_code="invalid_pdf"), 400
    if uploaded.stream.read(5) != b"%PDF-":
        return jsonify(error="The file does not appear to be a valid PDF", error_code="invalid_pdf"), 400
    uploaded.stream.seek(0)
    job_id = uuid.uuid4().hex
    directory = job_directory(job_id)
    directory.mkdir(parents=True, mode=0o700)
    source = directory / SOURCE_FILE
    try:
        uploaded.save(source)
        if source.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
            raise RequestEntityTooLarge()
        config["source_hash"] = file_hash(source)
        store.create_job(job_id, filename, question, int(not is_default_question(question)), config)
    except Exception:
        source.unlink(missing_ok=True)
        directory.rmdir()
        raise
    worker.wake()
    event("job_created", job_id=job_id, filename=filename, bytes=source.stat().st_size,
          profile=config["profile"], custom_prompt=not is_default_question(question),
          chunk_tokens=config["chunk_tokens"], analysis_tokens=config["analysis_tokens"],
          merge_tokens=config["merge_tokens"], final_tokens=config["final_tokens"],
          report_language=config["report_language"])
    return jsonify(id=job_id, status="queued", configuration=job_config.public_config(config)), 201


@app.get("/api/jobs/<job_id>/live")
def job_live(job_id):
    """Return the latest live preview of job_id, or HTTP 404.

    The preview file is read as is: the model is never called. The preview is
    empty when live output is disabled or the file is missing or unreadable,
    and only a running job is reported as active. Reasoning is included only
    with include_reasoning=true.
    """
    job = store.get_job(job_id)
    if not job:
        abort(404)
    path = job_directory(job_id) / PREVIEW_FILE
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


@app.get("/api/jobs/<job_id>/metrics")
def job_metrics(job_id):
    """Return a job's attempts and step measurements, paged with limit (1-500) and before.

    Measurements never contain document text, prompts or model output.
    """
    if not store.get_job(job_id):
        abort(404)
    try:
        limit = int(request.args.get("limit", "200"))
        before = int(request.args["before"]) if "before" in request.args else None
        if not 1 <= limit <= 500 or (before is not None and not 0 < before < 2**63):
            raise ValueError()
    except ValueError:
        abort(400)
    return jsonify(store.metrics(job_id, limit, before))


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    """Queue a failed or cancelled job again, with its original configuration.

    Finished steps are reused. Return HTTP 200, 400 when the request has a
    body (a retry cannot change the settings), 404 for an unknown job, or 409
    when the job is neither failed nor cancelled or has used all its attempts.
    """
    if not store.get_job(job_id):
        abort(404)
    if request.data or request.form:
        return jsonify(error="Retry uses the original snapshot. Create a new job to change options.", error_code="immutable_config"), 400
    outcome, attempts = store.requeue(job_id, MAX_JOB_ATTEMPTS)
    if outcome == "limit":
        return jsonify(error="The retry limit was reached. Create a new analysis.", error_code="retry_limit"), 409
    if outcome == "conflict":
        return jsonify(error="Only failed or cancelled jobs can be retried", error_code="retry_conflict"), 409
    worker.wake()
    legacy = store.load_config(job_id) is None
    event("job_retry", job_id=job_id, attempt=attempts + 1, status="queued", legacy=legacy)
    return jsonify(status="queued", warnings=["legacy_recomputed"] if legacy else [])


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    """Request the cancellation of a queued or running job.

    Return HTTP 202 once requested: a queued job is cancelled at once, a
    running job stops at its next cancellation check. Return 200 when the job
    is already cancelled, 404 for an unknown job and 409 for a finished one.
    """
    outcome, status = store.request_cancel(job_id)
    if outcome == "missing":
        abort(404)
    if outcome == "already_cancelled":
        return jsonify(status="cancelled"), 200
    if outcome == "not_active":
        return jsonify(error="This job cannot be cancelled.", error_code="cancel_conflict"), 409
    if outcome == "already_requested":
        return jsonify(status=status, cancel_requested=True), 202
    # The request is committed; let the running job see it without waiting for its next SQLite read.
    worker.notify_cancelled(job_id)
    event("job_cancel_requested", job_id=job_id, status="cancelled" if outcome == "cancelled" else "cancelling")
    return jsonify(status=status, cancel_requested=True), 202


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
    """Return (job, report path) for a finished job, or answer HTTP 404 or 409.

    The server chooses the path: a request can never name a file.
    """
    directory = job_directory(job_id)
    job = store.get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        abort(409)
    path = directory / REPORT_FILE
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        event("report_missing", logging.WARNING, job_id=job_id, status="unavailable")
        abort(404)
    return job, path


def ensure_report_name(job, path):
    """Return the report's download name.

    Jobs created before names were stored get one computed from the
    beginning of the report, which is then saved.
    """
    if job.get("report_name"):
        return job["report_name"]
    with path.open(encoding="utf-8", errors="replace") as stream:
        name = reports.report_filename(stream.read(reports.TITLE_SCAN_CHARS), job["filename"])
    store.set_report_name(job["id"], name)
    return name


@app.get("/api/jobs/<job_id>/report/view")
def view_report(job_id):
    """Render the report as HTML in the report viewer page.

    A report larger than reports.PREVIEW_MAX_BYTES is not rendered; it can
    still be downloaded.
    """
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


# Request hooks and error handlers ---------------------------------------------


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


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    """Return JSON with HTTP 413 for an oversized request body.

    The _ parameter is Flask's exception and is unused here.
    """
    event("upload_rejected", logging.WARNING, error_code="upload_limit")
    return jsonify(error=f"PDF too large (limit: {MAX_UPLOAD_MB} MiB)", error_code="upload_limit"), 413


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
    """Answer HTTP 400, 404, 409 and 500 with a generic JSON error, without exception details."""
    code = getattr(exc, "code", 500)
    messages = {400: "Invalid request.", 404: "Job or file not found.",
                409: "The result is not ready.", 500: "An internal error occurred."}
    return jsonify(error=messages.get(code, "Request failed."), error_code=f"http_{code}"), code


if __name__ == "__main__":
    event("processor_initializing", status="starting")
    try:
        job_config.resolve(runtime_defaults())
    except job_config.ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    try:
        store.initialize()
    except SchemaVersionError as exc:
        raise SystemExit(f"Database error: {exc}") from None
    # SIGTERM also interrupts a running job cooperatively, without cancelling it permanently.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        worker.start()
        log.info("processor_started port=%s", APP_PORT)
        serve(app, host="0.0.0.0", port=APP_PORT, threads=4)
    finally:
        stopped = worker.stop()
        store.close()
        event("processor_stopped", status="stopped", worker_stopped=stopped)
