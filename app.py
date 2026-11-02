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
import subprocess
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from document_processor import config as job_config, jobs as job_store, profiles, prompts, pipeline
from document_processor.model_client import ModelClient as BackendClient, ThinkingSplitter, ModelError, Cancelled
from flask import Flask, abort, jsonify, render_template, request, send_file, g
from urllib.parse import urlsplit
from pypdf import PdfReader
import pymupdf
from waitress import serve
from werkzeug.exceptions import RequestEntityTooLarge

# Read the configuration once when the process starts.
# Docker Compose passes variables from its .env file to the container.
APP_PORT = int(os.getenv("APP_PORT", "9932"))
DATA = Path(os.getenv("DATA_DIR", "/app/data"))
LLAMA_BASE = os.getenv("LLAMA_BASE_URL", "http://host.docker.internal:8080").rstrip("/")
KEY = os.getenv("LLAMA_API_KEY", "")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "120"))
CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "3000"))
CONTEXT_TOKENS = int(os.getenv("CONTEXT_TOKENS", "16384"))
OUTPUT_LANGUAGE = os.getenv("OUTPUT_LANGUAGE", "auto").strip() or "auto"
ANALYSIS_TOKENS = int(os.getenv("ANALYSIS_TOKENS", "1000"))
MERGE_TOKENS = int(os.getenv("MERGE_TOKENS", "1200"))
FINAL_TOKENS = int(os.getenv("FINAL_TOKENS", "1800"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "21600"))
LIVE_OUTPUT_ENABLED = os.getenv("LIVE_OUTPUT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
LIVE_PREVIEW_CHARS = max(1000, min(int(os.getenv("LIVE_PREVIEW_CHARS", "12000")), 50000))
MAX_QUESTION_CHARS = 1500
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "fra+eng")
OCR_DPI = int(os.getenv("OCR_DPI", "150"))
OCR_MAX_PIXELS = int(os.getenv("OCR_MAX_PIXELS", "5000000"))
OCR_TIMEOUT = int(os.getenv("OCR_TIMEOUT_SECONDS", "180"))
MIN_USABLE_CHARS = 35
DB_PATH = DATA / "jobs.sqlite3"
SYSTEM_FILE = Path(os.getenv("SYSTEM_PROMPT_FILE", "/app/config/system_prompt.txt"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("document-processor")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024 + 65536
worker_wakeup = threading.Event()


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
    with db_conn() as con:
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
        job_store.migrate(con)
        con.execute("UPDATE jobs SET status='cancelled', stage='Cancelled', phase_code='cancelled' WHERE cancel_requested=1 AND status IN ('queued','running')")
        con.execute("UPDATE jobs SET status='queued', stage='Resuming after restart', queued_at=? WHERE status='running'", (utcnow(),))


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
               "phase_code", "phase_args", "error_code", "cancel_requested", "queued_at"}
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


def serialize_job(row):
    """Convert a SQLite row into a consistent API dictionary.

    row must contain at least the status and progress_percent fields.
    Present every ``done`` job at 100 percent, even if an older database
    still stores zero. This normalization does not rewrite the SQLite row.
    """
    public = {"id", "filename", "question", "status", "stage", "pages", "total_chunks", "done_chunks",
              "error", "created_at", "updated_at", "progress_percent", "phase_code", "phase_args",
              "error_code", "cancel_requested", "queued_at", "has_active_jobs"}
    job = {key: row[key] for key in row.keys() if key in public}
    job["phase_args"] = json.loads(job.get("phase_args") or "{}")
    # The migration also initialized older completed jobs to 0 percent.
    if job["status"] == "done":
        job["progress_percent"] = 100
    return job


def get_job(job_id):
    """Return the normalized job matching job_id, or None."""
    with db_conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = serialize_job(row)
    config = job_store.load_config(db_conn, job_id)
    if config:
        job["configuration"] = job_config.public_config(config)
    return job


def job_dir(job_id):
    """Build a job directory path without creating the directory.

    job_id must contain 32 lowercase hexadecimal characters. Otherwise,
    abort the request with HTTP 404. This validation prevents using an ID as
    an arbitrary path. It does not check whether the job or directory exists.
    """
    # IDs are generated by the application, never from the PDF filename.
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        abort(404)
    return DATA / "jobs" / job_id


def atomic_text(path, text):
    """Atomically replace a file with UTF-8 text through a temporary file.

    path is a path or Path object whose parent must already exist. Write to
    a neighboring file ending in .tmp first, then move it with os.replace so
    readers never see partial content. Return no value and propagate I/O
    errors.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    except OSError:
        raise job_config.ConfigurationError("missing_prompt", "The system prompt is missing or unreadable.") from None


def runtime_defaults():
    """Read administrative defaults; snapshots isolate existing jobs from changes."""
    adapter = os.getenv("LLAMA_THINKING_ADAPTER", "unsupported")
    if adapter not in ("unsupported", "chat_template", "prompt"):
        raise job_config.ConfigurationError("invalid_adapter", "Invalid LLAMA_THINKING_ADAPTER.")
    enabled = lambda name: os.getenv(name, "false").lower() in ("1", "true", "yes", "on")
    return {
        "chunk_tokens": CHUNK_TOKENS, "context_tokens": CONTEXT_TOKENS,
        "analysis_tokens": ANALYSIS_TOKENS, "merge_tokens": MERGE_TOKENS,
        "final_tokens": FINAL_TOKENS, "report_language": OUTPUT_LANGUAGE,
        "temperature": 0.2, "top_p": None, "top_k": None, "min_p": None,
        "thinking_section": "inherit", "thinking_merge": "inherit", "thinking_final": "inherit",
        "thinking_adapter": adapter, "initial_thinking": enabled("LLAMA_INITIAL_THINKING"),
        "apply_template": enabled("LLAMA_APPLY_TEMPLATE_SUPPORTED"),
        "usage_supported": enabled("LLAMA_USAGE_SUPPORTED"),
        "request_timeout": REQUEST_TIMEOUT,
        "step_timeout_seconds": int(os.getenv("STEP_TIMEOUT_SECONDS", str(REQUEST_TIMEOUT))),
        "max_calls": int(os.getenv("MAX_MODEL_CALLS", "500")),
        "max_attempts": int(os.getenv("MAX_JOB_ATTEMPTS", "10")),
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


def output_language_instruction():
    """Build the language instruction from OUTPUT_LANGUAGE.

    In ``auto`` mode, request the language used by the user's question.
    Otherwise, explicitly request the configured language.
    """
    if OUTPUT_LANGUAGE.lower() == "auto":
        return "Write in the same language as the user's request."
    return f"Write in {OUTPUT_LANGUAGE}."


def split_with_page(text, page, limit=5000):
    """Split text into fragments carrying the page number.

    text contains raw text and page is its unmodified page number. limit
    bounds each fragment in characters, not tokens. Prefer paragraph breaks,
    then line breaks and spaces when choosing a boundary. Return non-empty
    ``page``/``text`` dictionaries.
    """
    out = []
    text = text.strip()
    while text:
        if len(text) <= limit:
            out.append({"page": page, "text": text})
            break
        end = limit
        # Look for a natural separator near the end of the allowed fragment.
        for marker in ("\n\n", "\n", " "):
            cut = text.rfind(marker, int(limit * 0.60), limit)
            if cut > 0:
                end = cut + len(marker)
                break
        # Keep a short Markdown/numbered heading attached to its following text.
        prefix = text[:end].rstrip("\n")
        line_start = prefix.rfind("\n") + 1
        last_line = prefix[line_start:].strip()
        if line_start >= limit * 0.6 and re.match(r"^(#{1,6}\s|\d+(?:\.\d+)*[.)]?\s+[A-Z])", last_line) and len(last_line) < 120:
            end = line_start
        out.append({"page": page, "text": text[:end].strip()})
        text = text[end:].strip()
    return [v for v in out if v["text"]]


def make_chunks(pages, model, budget=None):
    """Build sections bounded by CHUNK_TOKENS.

    pages is a list of ``page``/``text`` dictionaries from extraction. model
    must provide tokenize to count tokens. Prefix each passage with its page
    reference, then group passages while the budget allows. Split passages
    that are still too long.

    Return ``text``/``pages`` dictionaries; pages contains sorted unique page
    numbers. Raise RuntimeError if a very small passage still exceeds the
    budget. Propagate errors from the remote tokenizer.
    """
    budget = CHUNK_TOKENS if budget is None else budget
    units = []
    for p in pages:
        units.extend(split_with_page(p["text"], p["page"]))
    chunks = []
    current = []

    def chunk_text(parts):
        """Join parts with page markers."""
        return "\n\n".join(f"[Page {x['page']}]\n{x['text']}" for x in parts)

    for u in units:
        # Count actual tokens: density depends on the tokenizer.
        pending = [u]
        while pending:
            piece = pending.pop(0)
            solo = chunk_text([piece])
            if model.tokenize(solo) > budget:
                if len(piece["text"]) < 50:
                    raise RuntimeError("Unable to split a PDF passage into sufficiently small chunks")
                half = len(piece["text"]) // 2
                pivot = -1
                for boundary in ("\n\n", "\n", " "):
                    pivot = piece["text"].rfind(boundary, max(0, half-300), half+300)
                    if pivot > 0:
                        break
                if pivot < 1:
                    pivot = half
                pending.insert(0, {"page": piece["page"], "text": piece["text"][pivot:].strip()})
                pending.insert(0, {"page": piece["page"], "text": piece["text"][:pivot].strip()})
                continue
            candidate = current + [piece]
            if current and model.tokenize(chunk_text(candidate)) > budget:
                chunks.append({"text": chunk_text(current), "pages": sorted(set(v["page"] for v in current))})
                current = [piece]
            else:
                current = candidate
    if current:
        chunks.append({"text": chunk_text(current), "pages": sorted(set(v["page"] for v in current))})
    return chunks


def normalize_extracted_text(text):
    """Clean text while preserving its paragraphs.

    Treat None as empty text. Remove null characters, spaces after newlines,
    and leading or trailing whitespace.
    """
    text = (text or "").replace("\x00", "")
    return re.sub(r"\n[ \t]+", "\n", text).strip()


def text_is_usable(text):
    """Estimate whether text is usable for analysis.

    Require MIN_USABLE_CHARS alphanumeric characters and limit Unicode
    replacement signs, which can indicate corrupted extraction. Return a
    boolean; this check does not measure scientific quality.
    """
    alnum = sum(c.isalnum() for c in text)
    if alnum < MIN_USABLE_CHARS:
        return False
    replacement = text.count("\ufffd")
    return replacement < max(5, alnum // 10)


def ocr_page(page, number, settings=None):
    """Recognize a PyMuPDF page with local Tesseract.

    page must belong to an open document; number is used in error messages.
    Return cleaned text, or an empty string when OCR is disabled. Adapt the
    resolution to the OCR_MAX_PIXELS budget before allocation, then
    run Tesseract with OCR_LANGUAGES.

    Raise RuntimeError when Tesseract is missing, fails, or exceeds
    OCR_TIMEOUT. Page rendering errors are propagated to the caller.
    """
    settings = settings or runtime_defaults()["extraction"]
    if not settings["enabled"]:
        return ""
    # Reduce the render before allocation to limit memory use.
    pixels_at_dpi = (page.rect.width * settings["dpi"] / 72) * (page.rect.height * settings["dpi"] / 72)
    scale = min(1.0, math.sqrt(settings["max_pixels"] / max(1.0, pixels_at_dpi)))
    dpi = max(1, math.floor(settings["dpi"] * scale))
    if (page.rect.width * dpi / 72) * (page.rect.height * dpi / 72) > settings["max_pixels"]:
        raise ValueError("PDF page dimensions exceed the OCR pixel budget.")
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    try:
        png = pix.tobytes("png")
    finally:
        del pix
    try:
        process = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", settings["languages"], "--psm", "3"],
            input=png, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=settings["timeout"], check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Tesseract is missing from the container; rebuild the Docker image") from exc
    except subprocess.TimeoutExpired as exc:
        raise ModelError("ocr_timeout", f"OCR timed out on page {number}.") from None
    finally:
        del png
    if process.returncode:
        raise ModelError("ocr_failed", f"OCR failed on page {number}. Check installed language packs.")
    return normalize_extracted_text(process.stdout.decode("utf-8", errors="replace"))


def extract_pdf(pdf_path, progress=None, settings=None):
    """Extract a PDF and report pages with insufficient text.

    pdf_path identifies the source file. Try PyMuPDF, then pypdf when text is
    insufficient, and finally OCR when enabled. The optional progress
    callback receives a label during extraction and page OCR.

    Return a tuple containing page/text dictionaries, total page count,
    insufficient page numbers, and OCR page numbers. Keep non-empty text from
    pages judged insufficient.

    Raise ValueError when the PDF is unreadable, password-protected, exceeds
    MAX_PAGES, or contains no usable text. Propagate OCR errors; the document
    context closes it on exit.
    """
    settings = settings or runtime_defaults()["extraction"]
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise ValueError("Unable to read PDF.") from None
    with doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF: remove the password before uploading")
        count = len(doc)
        if count > settings["max_pages"]:
            raise ValueError(f"PDF has {count} pages (configured maximum exceeded).")
        pages, missing, ocr_pages = [], [], []
        alt_reader = None
        for number in range(1, count + 1):
            if progress:
                progress(f"Extracting text: page {number}/{count}")
            page = doc[number - 1]
            try:
                text = normalize_extracted_text(page.get_text("text", sort=True))
            except Exception as exc:
                log.warning("extraction_fallback page=%d reader=pymupdf", number)
                text = ""
            if not text_is_usable(text):
                # Try another reader for unusual or incorrectly encoded fonts.
                try:
                    if alt_reader is None:
                        alt_reader = PdfReader(str(pdf_path), strict=False)
                    alternate = normalize_extracted_text(alt_reader.pages[number - 1].extract_text())
                    if text_is_usable(alternate) or len(alternate) > len(text):
                        text = alternate
                except Exception as exc:
                    log.warning("extraction_fallback page=%d reader=pypdf", number)
            if not text_is_usable(text) and settings["enabled"]:
                if progress:
                    progress(f"OCR recognition: page {number}/{count}")
                detected = ocr_page(page, number, settings)
                ocr_pages.append(number)
                if text_is_usable(detected) or len(detected) > len(text):
                    text = detected
            if not text_is_usable(text):
                missing.append(number)
            if text:
                pages.append({"page": number, "text": text})
            # Drop the page reference before moving to the next page.
            del page
        if not any(text_is_usable(p["text"]) for p in pages):
            detail = "even after OCR" if settings["enabled"] else "(OCR disabled)"
            raise ValueError(f"No usable text found in this PDF {detail}. Check the source PDF.")
        log.info("PDF extraction: %d pages, OCR pages=%s, insufficient pages=%s", count, ocr_pages, missing)
        return pages, count, missing, ocr_pages


def analyze_job(job_id):
    """Run the versioned pipeline while preserving the public Python entry point."""
    return pipeline.run(job_id, sys.modules[__name__])


def cancellation_requested(job_id):
    """Read cooperative cancellation without exposing private configuration."""
    with db_conn() as con:
        row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    return not row or bool(row["cancel_requested"])


def worker_loop():
    """Process jobs forever in creation order.

    Claim the next job by changing it from ``queued`` to ``running``, then
    call analyze_job. On error, record ``failed`` and an error message limited
    to 800 characters while keeping produced files. When idle, wait for a
    worker_wakeup signal or a six-second timeout. This loop belongs to the
    application's only worker thread.
    """
    while True:
        try:
            with db_conn() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
                if row:
                    con.execute("UPDATE jobs SET status='running', stage='Starting', updated_at=? WHERE id=?", (utcnow(), row["id"]))
            if row:
                job_id = row["id"]
                try:
                    analyze_job(job_id)
                except Cancelled:
                    update_job(job_id, status="cancelled", stage="Cancelled", phase_code="cancelled", error="", error_code="cancelled")
                except Exception as exc:
                    code = getattr(exc, "code", "analysis_failed")
                    log.error("job_failed job_id=%s code=%s type=%s", job_id, code, type(exc).__name__)
                    message = str(exc) if isinstance(exc, (ModelError, job_config.ConfigurationError)) else "Analysis failed. Check the PDF, configuration and local diagnostics."
                    update_job(job_id, status="failed", stage="Error", phase_code="failed", error_code=code, error=message)
                    # Keep the last preview to help diagnose the error.
                continue
        except Exception:
            log.error("worker_database_error")
        worker_wakeup.wait(timeout=6)
        worker_wakeup.clear()


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    """Return JSON with HTTP 413 for an oversized request body.

    The _ parameter is Flask's exception and is unused here.
    """
    return jsonify(error=f"PDF too large (limit: {MAX_UPLOAD_MB} MiB)", error_code="upload_limit"), 413


@app.get("/")
def index():
    """Serve the HTML page and its PDF size limit."""
    translations = json.loads((Path(__file__).parent / "static" / "translations.json").read_text(encoding="utf-8"))
    return render_template("index.html", limit=MAX_UPLOAD_MB, translations=translations)


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
    has_active_jobs = bool(rows and rows[0]["has_active_jobs"])
    jobs = [serialize_job(row) for row in rows]
    with db_conn() as con:
        configs = {row["job_id"]: json.loads(row["snapshot"]) for row in con.execute(
            "SELECT job_id, snapshot FROM job_configs WHERE job_id IN (SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT 100)")}
    for job in jobs:
        job.pop("has_active_jobs")
        if job["id"] in configs:
            job["configuration"] = job_config.public_config(configs[job["id"]])
    return jsonify(jobs=jobs, has_active_jobs=has_active_jobs)


@app.get("/api/jobs/<job_id>")
def job_info(job_id):
    """Return job_id as JSON, or HTTP 404 when it does not exist."""
    job = get_job(job_id)
    if not job:
        abort(404)
    return jsonify(job)


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
    question = (request.form.get("question") or "Analyze the entire document").strip()
    if not uploaded or not uploaded.filename:
        return jsonify(error="Select a PDF file", error_code="invalid_pdf"), 400
    if not question or len(question) > MAX_QUESTION_CHARS:
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
            con.execute("""INSERT INTO jobs (id, filename, question, status, stage, created_at, updated_at)
              VALUES (?, ?, ?, 'queued', 'Queued', ?, ?)""", (job_id, filename, question, utcnow(), utcnow()))
            con.execute("UPDATE jobs SET queued_at=? WHERE id=?", (utcnow(), job_id))
            job_store.save_config(con, job_id, config)
    except Exception:
        (directory / "source.pdf").unlink(missing_ok=True)
        directory.rmdir()
        raise
    worker_wakeup.set()
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
    return jsonify(status="queued", warnings=["legacy_recomputed"] if legacy else [])



@app.get("/api/jobs/<job_id>/report")
def report(job_id):
    """Send the completed report as a UTF-8 Markdown attachment.

    job_id determines rapport_final.md and the download name
    ``analysis_<first 8 characters>.md``. Return HTTP 404 when the job or
    file is missing, and 409 when the job is not in ``done`` state.
    """
    job = get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        abort(409)
    path = job_dir(job_id) / "rapport_final.md"
    if not path.is_file() or path.resolve().parent != job_dir(job_id).resolve():
        abort(404)
    if path.stat().st_size == 0:
        abort(409)
    return send_file(path, mimetype="text/markdown", as_attachment=True,
                     download_name="analysis_" + job_id[:8] + ".md")


_capability_cache = {"value": None, "expires": 0}
_capability_lock = threading.Lock()


def cached_capabilities():
    """Read an advisory cache without initiating network traffic during form submission."""
    if time.monotonic() < _capability_cache["expires"]:
        return _capability_cache["value"]
    return None


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
    if request.path.endswith("/report"):
        log.info("report_response status=%d duration_ms=%.1f bytes=%s", response.status_code,
                 (time.monotonic() - g.started) * 1000, response.content_length)
    return response


@app.errorhandler(job_config.ConfigurationError)
def invalid_configuration(exc):
    """Return stable validation codes for translated browser errors."""
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
            finally:
                client.session.close()
            _capability_cache.update(value=value, expires=time.monotonic() + 30)
    process_rss_kib = None
    try:
        matched = re.search(r"^VmRSS:\s+(\d+)", Path("/proc/self/status").read_text(), re.M)
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
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            abort(404)
        if row["status"] == "cancelled":
            return jsonify(status="cancelled"), 200
        if row["status"] not in ("queued", "running"):
            return jsonify(error="This job cannot be cancelled.", error_code="cancel_conflict"), 409
        queued = row["status"] == "queued"
        con.execute("UPDATE jobs SET cancel_requested=1, status=?, phase_code=?, stage=?, updated_at=? WHERE id=?",
                    ("cancelled" if queued else "running", "cancelled" if queued else "cancelling",
                     "Cancelled" if queued else "Cancellation requested", utcnow(), job_id))
    worker_wakeup.set()
    return jsonify(status="cancelled" if queued else "running", cancel_requested=True), 202


if __name__ == "__main__":
    job_config.resolve(runtime_defaults())
    init_db()
    threading.Thread(target=worker_loop, name="pdf-worker", daemon=True).start()
    log.info("processor_started port=%s", APP_PORT)
    serve(app, host="0.0.0.0", port=APP_PORT, threads=4)
