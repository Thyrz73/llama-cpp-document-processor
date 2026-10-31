"""Lightweight, persistent, sequential PDF analyzer with selective local OCR.

No embeddings, Redis, GPU or additional language models.
"""
from __future__ import annotations

import json
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

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file
from pypdf import PdfReader
import pymupdf
from waitress import serve
from werkzeug.exceptions import RequestEntityTooLarge

APP_PORT = int(os.getenv("APP_PORT", "9932"))
DATA = Path(os.getenv("DATA_DIR", "/app/data"))
LLAMA_BASE = os.getenv("LLAMA_BASE_URL", "http://host.docker.internal:8080").rstrip("/")
KEY = os.getenv("LLAMA_API_KEY", "")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "120"))
CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "3000"))
CONTEXT_TOKENS = int(os.getenv("CONTEXT_TOKENS", "8192"))
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_conn():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    return con


def init_db():
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
        con.execute("UPDATE jobs SET status='queued', stage='Resuming after restart' WHERE status='running'")


def update_job(job_id, **values):
    if not values:
        return
    values["updated_at"] = utcnow()
    parts = ", ".join(f"{k}=?" for k in values)
    with db_conn() as con:
        con.execute(f"UPDATE jobs SET {parts} WHERE id=?", [*values.values(), job_id])


def get_job(job_id):
    with db_conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def job_dir(job_id):
    # IDs are generated internally, not built from the uploaded filename.
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        abort(404)
    return DATA / "jobs" / job_id


def atomic_text(path, text):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_system_prompt():
    return SYSTEM_FILE.read_text(encoding="utf-8").strip()


class ModelClient:
    def __init__(self):
        self.session = requests.Session()
        if KEY:
            self.session.headers.update({"Authorization": f"Bearer {KEY}"})
        self.model_name = None

    def json_request(self, method, path, *, timeout=90, **kwargs):
        response = self.session.request(method, LLAMA_BASE + path,
                                        timeout=(10, timeout), **kwargs)
        response.raise_for_status()
        return response.json()

    def tokenize(self, text):
        payload = self.json_request("POST", "/tokenize", json={"content": text, "add_special": False})
        if "tokens" not in payload or not isinstance(payload["tokens"], list):
            raise RuntimeError("Unexpected response from /tokenize")
        return len(payload["tokens"])

    def model(self):
        if self.model_name:
            return self.model_name
        payload = self.json_request("GET", "/v1/models")
        items = payload.get("data", [])
        if not items:
            raise RuntimeError("No model returned by /v1/models")
        self.model_name = items[0]["id"]
        return self.model_name

    def complete(self, instruction, content, max_tokens, *, on_update=None):
        """Stream the completion to the worker; optionally publish a bounded live preview.

        SSE is read by the background worker, not by the browser. Closing the
        browser therefore never disconnects or cancels a model request.
        """
        prompt = read_system_prompt()
        estimated = self.tokenize(prompt + "\n" + instruction + "\n" + content)
        if estimated + max_tokens + 384 > CONTEXT_TOKENS:
            raise ValueError(
                f"Prompt would exceed configured context ({estimated} input + "
                f"{max_tokens} output + 384 reserved > {CONTEXT_TOKENS}). "
                "Lower CHUNK_TOKENS, output tokens, or increase CONTEXT_TOKENS."
            )
        payload = {
            "model": self.model(),
            "stream": True,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": instruction + "\n\n" + content},
            ],
        }
        splitter = ThinkingSplitter()
        reasoning = []
        latest = 0.0
        completed = False
        finish_reason = None

        def publish(force=False):
            nonlocal latest
            if on_update is None:
                return
            now = time.monotonic()
            if not force and now - latest < 0.8:
                return
            latest = now
            on_update(splitter.content, splitter.reasoning + "".join(reasoning), completed)

        # Requests.Session.request(stream=True) keeps exactly one HTTP stream
        # open while llama.cpp generates. No extra inference or thread is used.
        with self.session.post(
            LLAMA_BASE + "/v1/chat/completions",
            json=payload, stream=True, timeout=(10, REQUEST_TIMEOUT)
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines(chunk_size=64, decode_unicode=True):
                if not line:
                    continue
                if not isinstance(line, str):
                    line = line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                message = line[5:].strip()
                if message == "[DONE]":
                    completed = True
                    break
                try:
                    event = json.loads(message)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Malformed llama.cpp streaming response") from exc
                if "error" in event:
                    raise RuntimeError(f"llama.cpp streaming error: {str(event['error'])[:300]}")
                for choice in event.get("choices", []):
                    delta = choice.get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        splitter.feed(delta["content"])
                    for field in ("reasoning_content", "reasoning"):
                        part = delta.get(field)
                        if isinstance(part, str) and part:
                            reasoning.append(part)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                publish()
        splitter.finish()
        if finish_reason == "length":
            raise RuntimeError("Model response hit max_tokens; increase its output-token budget to avoid truncated analysis")
        if finish_reason is not None:
            completed = True
        # A normal stream should have either an explicit [DONE] or a finish
        # reason. Treat silent truncation as an error; never save partial work
        # as if the whole section had completed.
        if not completed and finish_reason is None:
            raise RuntimeError("llama.cpp stream closed before completion")
        publish(force=True)
        answer = splitter.content.strip()
        if not answer:
            raise RuntimeError("Model returned empty content; try increasing the output-token budget")
        return answer


class ThinkingSplitter:
    """Split raw <think> blocks if the model does not expose reasoning fields.

    Think tags may be split across multiple SSE chunks. This only separates
    content explicitly generated by the model; it does not infer hidden thoughts.
    """
    def __init__(self):
        self.content_parts = []
        self.reasoning_parts = []
        self.pending = ""
        self.inside_think = False

    @property
    def content(self):
        return "".join(self.content_parts)

    @property
    def reasoning(self):
        return "".join(self.reasoning_parts)

    def feed(self, fragment):
        self.pending += fragment
        while self.pending:
            marker = "</think>" if self.inside_think else "<think>"
            pos = self.pending.find(marker)
            target = self.reasoning_parts if self.inside_think else self.content_parts
            if pos >= 0:
                if pos:
                    target.append(self.pending[:pos])
                self.pending = self.pending[pos + len(marker):]
                self.inside_think = not self.inside_think
                continue
            # Retain a partial marker at the boundary of two SSE events.
            keep = 0
            for size in range(1, min(len(marker), len(self.pending) + 1)):
                if self.pending.endswith(marker[:size]):
                    keep = size
            if len(self.pending) > keep:
                target.append(self.pending[:len(self.pending) - keep])
            self.pending = self.pending[len(self.pending) - keep:]
            break

    def finish(self):
        if self.pending:
            (self.reasoning_parts if self.inside_think else self.content_parts).append(self.pending)
            self.pending = ""


def output_language_instruction():
    if OUTPUT_LANGUAGE.lower() == "auto":
        return "Write in the same language as the user's request."
    return f"Write in {OUTPUT_LANGUAGE}."


def split_with_page(text, page, limit=5000):
    """Split potentially massive pages at newline/space boundaries, retaining page ID."""
    out = []
    text = text.strip()
    while text:
        if len(text) <= limit:
            out.append({"page": page, "text": text})
            break
        end = limit
        # Prefer paragraph boundaries when present, otherwise ordinary whitespace.
        for marker in ("\n\n", "\n", " "):
            cut = text.rfind(marker, int(limit * 0.60), limit)
            if cut > 0:
                end = cut + len(marker)
                break
        out.append({"page": page, "text": text[:end].strip()})
        text = text[end:].strip()
    return [v for v in out if v["text"]]


def make_chunks(pages, model):
    units = []
    for p in pages:
        units.extend(split_with_page(p["text"], p["page"]))
    chunks = []
    current = []

    def chunk_text(parts):
        return "\n\n".join(f"[Page {x['page']}]\n{x['text']}" for x in parts)

    for u in units:
        # Some tokenizers are denser than chars/4, so bisect if needed.
        pending = [u]
        while pending:
            piece = pending.pop(0)
            solo = chunk_text([piece])
            if model.tokenize(solo) > CHUNK_TOKENS:
                if len(piece["text"]) < 50:
                    raise RuntimeError("Unable to split a PDF passage into sufficiently small chunks")
                half = len(piece["text"]) // 2
                pivot = piece["text"].rfind(" ", max(0, half-300), half+300)
                if pivot < 1:
                    pivot = half
                pending.insert(0, {"page": piece["page"], "text": piece["text"][pivot:].strip()})
                pending.insert(0, {"page": piece["page"], "text": piece["text"][:pivot].strip()})
                continue
            candidate = current + [piece]
            if current and model.tokenize(chunk_text(candidate)) > CHUNK_TOKENS:
                chunks.append({"text": chunk_text(current), "pages": sorted(set(v["page"] for v in current))})
                current = [piece]
            else:
                current = candidate
    if current:
        chunks.append({"text": chunk_text(current), "pages": sorted(set(v["page"] for v in current))})
    return chunks


def normalize_extracted_text(text):
    """Light cleanup; preserve paragraphs and page boundaries."""
    text = (text or "").replace("\x00", "")
    return re.sub(r"\n[ \t]+", "\n", text).strip()


def text_is_usable(text):
    """Avoid accepting a small cover-page label or corrupt font mapping as full text."""
    alnum = sum(c.isalnum() for c in text)
    if alnum < MIN_USABLE_CHARS:
        return False
    replacement = text.count("\ufffd")
    return replacement < max(5, alnum // 10)


def ocr_page(page, number):
    """Render grayscale at bounded resolution, OCR in one Tesseract subprocess."""
    if not OCR_ENABLED:
        return ""
    # Never render a giant image: cap the number of pixels before allocation.
    pixels_at_dpi = (page.rect.width * OCR_DPI / 72) * (page.rect.height * OCR_DPI / 72)
    scale = min(1.0, math.sqrt(OCR_MAX_PIXELS / max(1.0, pixels_at_dpi)))
    dpi = max(72, round(OCR_DPI * scale))
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    try:
        png = pix.tobytes("png")
    finally:
        del pix
    try:
        process = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", OCR_LANGUAGES, "--psm", "3"],
            input=png, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=OCR_TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Tesseract is missing from the container; rebuild the Docker image") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"OCR timed out on page {number} (over {OCR_TIMEOUT}s)") from exc
    finally:
        del png
    if process.returncode:
        details = process.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise RuntimeError(f"OCR failed on page {number}: {details}")
    return normalize_extracted_text(process.stdout.decode("utf-8", errors="replace"))


def extract_pdf(pdf_path, progress=None):
    """Fast digital extraction, pypdf fallback, then OCR only for problematic pages.

    Return pages, count, missing pages and pages actually processed with OCR.
    """
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise ValueError(f"Unable to read PDF: {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF: remove the password before uploading")
        count = len(doc)
        if count > MAX_PAGES:
            raise ValueError(f"PDF has {count} pages (configured maximum: {MAX_PAGES})")
        pages, missing, ocr_pages = [], [], []
        alt_reader = None
        for number in range(1, count + 1):
            if progress:
                progress(f"Extracting text: page {number}/{count}")
            page = doc[number - 1]
            try:
                text = normalize_extracted_text(page.get_text("text", sort=True))
            except Exception as exc:
                log.warning("PyMuPDF extraction page %d: %s", number, exc)
                text = ""
            if not text_is_usable(text):
                # Alternative for PDFs with unusual text layers / font encodings.
                try:
                    if alt_reader is None:
                        alt_reader = PdfReader(str(pdf_path), strict=False)
                    alternate = normalize_extracted_text(alt_reader.pages[number - 1].extract_text())
                    if text_is_usable(alternate) or len(alternate) > len(text):
                        text = alternate
                except Exception as exc:
                    log.warning("pypdf fallback page %d: %s", number, exc)
            if not text_is_usable(text) and OCR_ENABLED:
                if progress:
                    progress(f"OCR recognition: page {number}/{count}")
                detected = ocr_page(page, number)
                ocr_pages.append(number)
                if text_is_usable(detected) or len(detected) > len(text):
                    text = detected
            if not text_is_usable(text):
                missing.append(number)
            if text:
                pages.append({"page": number, "text": text})
            # Explicitly drop references before the next page to minimize peak RAM.
            del page
        if not any(text_is_usable(p["text"]) for p in pages):
            detail = "even after OCR" if OCR_ENABLED else "(OCR disabled)"
            raise ValueError(f"No usable text found in this PDF {detail}. Check the source PDF.")
        log.info("PDF extraction: %d pages, OCR pages=%s, insufficient pages=%s", count, ocr_pages, missing)
        return pages, count, missing, ocr_pages


def analyse_job(job_id):
    directory = job_dir(job_id)
    job = get_job(job_id)
    model = ModelClient()
    live_path = directory / "live_preview.json"
    if LIVE_OUTPUT_ENABLED:
        atomic_json(live_path, {"phase": "Preparing document", "content": "", "reasoning": "", "active": False, "updated_at": utcnow()})

    def live_callback(phase):
        if not LIVE_OUTPUT_ENABLED:
            return None
        # Only a bounded preview is kept, to minimize disk writes and avoid
        # exposing an entire document or large internal debug files via HTTP.
        def save(content, reasoning, completed):
            atomic_json(live_path, {
                "phase": phase,
                "content": content[-LIVE_PREVIEW_CHARS:],
                "reasoning": reasoning[-LIVE_PREVIEW_CHARS:],
                "content_chars": len(content),
                "reasoning_chars": len(reasoning),
                "active": not completed,
                "updated_at": utcnow(),
            })
        save("", "", False)
        return save

    update_job(job_id, stage="Extracting PDF", progress_percent=2)
    extracted_file = directory / "extracted.json"
    previous = json.loads(extracted_file.read_text(encoding="utf-8")) if extracted_file.exists() else None
    if previous is not None and previous.get("extraction_version") == 2:
        pages, page_count, missing = previous["pages"], previous["page_count"], previous["missing"]
        ocr_pages = previous.get("ocr_pages", [])
    else:
        # Old extractions must not be paired with chunks/analyses produced
        # before the OCR update: page contents and chunk boundaries can change.
        for pattern in ("chunks.json", "analysis_*.md", "merge_*.md", "rapport_final.md"):
            for obsolete in directory.glob(pattern):
                obsolete.unlink()
        def extraction_progress(stage):
            matched = re.search(r"page (\d+)/(\d+)", stage)
            percent = 2 + round(5 * int(matched[1]) / max(1, int(matched[2]))) if matched else 2
            update_job(job_id, stage=stage, progress_percent=percent)

        pages, page_count, missing, ocr_pages = extract_pdf(
            directory / "source.pdf", progress=extraction_progress
        )
        atomic_json(extracted_file, {"extraction_version": 2, "pages": pages,
                                     "page_count": page_count, "missing": missing,
                                     "ocr_pages": ocr_pages})
    update_job(job_id, pages=page_count, stage="Splitting document", progress_percent=7)
    chunks_file = directory / "chunks.json"
    if chunks_file.exists():
        chunks = json.loads(chunks_file.read_text(encoding="utf-8"))
    else:
        chunks = make_chunks(pages, model)
        atomic_json(chunks_file, chunks)
    if not chunks:
        raise ValueError("Document contains no analyzable text")
    update_job(job_id, total_chunks=len(chunks), progress_percent=10)

    notes = []
    for i, chunk in enumerate(chunks):
        dest = directory / f"analysis_{i:04d}.md"
        if not dest.exists():
            update_job(job_id, done_chunks=i, stage=f"Analyzing section {i+1}/{len(chunks)}", progress_percent=10 + round(70*i/len(chunks)))
            instructions = (
                "Analyze this document excerpt accurately in structured, factual notes. "
                "List only documented objectives, methods, findings, numerical values with "
                "units, hypotheses, conclusions, uncertainties, and limitations. "
                "For every important claim, include [Page N]. Do not invent missing "
                "details or draw premature document-wide conclusions. "
                + output_language_instruction() + "\n"
                + f"User request: {job['question']}\n"
                + f"Excerpt {i+1} of {len(chunks)}:"
            )
            answer = model.complete(instructions, chunk["text"], ANALYSIS_TOKENS,
                                    on_update=live_callback(f"Analyzing section {i+1}/{len(chunks)} · PDF pages: {', '.join(map(str, chunk['pages'][:12]))}{'…' if len(chunk['pages']) > 12 else ''}"))
            atomic_text(dest, answer)
        notes.append(dest.read_text(encoding="utf-8"))
        update_job(job_id, done_chunks=i+1, stage=f"Sections analyzed: {i+1}/{len(chunks)}",
                   progress_percent=10 + round(70*(i+1)/len(chunks)))

    # Hierarchical merging ensures no final request exceeds the configured context.
    level = 0
    while len(notes) > 4:
        grouped = []
        for index in range(0, len(notes), 4):
            block = notes[index:index+4]
            if len(block) == 1:
                grouped.append(block[0])  # No model call needed for a single note.
                continue
            dest = directory / f"merge_{level:02d}_{index // 4:04d}.md"
            if not dest.exists():
                update_job(job_id, stage=f"Merging level {level+1}, group {index//4+1}/{(len(notes)+3)//4}",
                           progress_percent=min(94, 81 + level * 4))
                combined = "\n\n".join(f"## Notes {j+1}\n{part}" for j, part in enumerate(block))
                # Chunk outputs are limited in size, but check anyway before sending.
                if model.tokenize(combined) + MERGE_TOKENS + 1200 > CONTEXT_TOKENS:
                    raise RuntimeError("Merge input exceeds the configured context budget; reduce MERGE_TOKENS")
                answer = model.complete(
                    "Merge these analyses without inventing facts. Keep all distinct "
                    "results, numbers, and [Page N] references, while removing duplicates. "
                    "Do not draw conclusions absent from the notes. "
                    + output_language_instruction() + " "
                    + f"Original request: {job['question']}.",
                    combined,
                    MERGE_TOKENS,
                    on_update=live_callback(f"Merging level {level+1}, group {index//4+1}"),
                )
                atomic_text(dest, answer)
            grouped.append(dest.read_text(encoding="utf-8"))
        notes = grouped
        level += 1

    final_path = directory / "rapport_final.md"
    if not final_path.exists():
        update_job(job_id, stage="Writing final report", progress_percent=95)
        combined = "\n\n".join(f"### Intermediate synthesis {i+1}\n{note}" for i, note in enumerate(notes))
        if model.tokenize(combined) + FINAL_TOKENS + 1400 > CONTEXT_TOKENS:
            raise RuntimeError("Final input exceeds the configured context budget")
        warnings = ("Pages with insufficient extracted/OCR text: " + ", ".join(map(str, missing))) if missing else "No pages with insufficient text detected."
        if ocr_pages:
            warnings += " OCR was used on pages: " + ", ".join(map(str, ocr_pages)) + "."
        final = model.complete(
            "Write a final Markdown report answering the user's request. Structure: "
            "summary, document methodology, findings and figures, limitations, "
            "conclusions, and an explicit answer to the question. "
            "Cite [Page N] for important facts, mention missing evidence, and do not "
            "claim to have interpreted images or figures that were not extracted. "
            "Avoid repetitions. " + output_language_instruction() + "\n"
            + f"Question: {job['question']}\n"
            + f"Document: {job['filename']} ({page_count} pages).\n"
            + f"Extraction notes: {warnings}\n"
            + "Consolidated notes:",
            combined,
            FINAL_TOKENS,
            on_update=live_callback("Writing final report"),
        )
        header = f"# Analysis of {job['filename']}\n\n"
        header += f"*PDF pages: {page_count} — analyzed chunks: {len(chunks)}.*\n\n"
        if missing:
            header += f"> Warning: insufficient text on pages {', '.join(map(str, missing))} after extraction/OCR.\n\n"
        if ocr_pages:
            header += f"*OCR used on {len(ocr_pages)} page(s): {', '.join(map(str, ocr_pages))}.*\n\n"
        atomic_text(final_path, header + final + "\n")
    update_job(job_id, status="done", stage="Analysis completed", error="", progress_percent=100)
    log.info("Job %s finished (%d pages; %d chunks)", job_id, page_count, len(chunks))


def worker_loop():
    while True:
        try:
            with db_conn() as con:
                row = con.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
                if row:
                    con.execute("UPDATE jobs SET status='running', stage='Starting', updated_at=? WHERE id=?", (utcnow(), row["id"]))
            if row:
                job_id = row["id"]
                try:
                    analyse_job(job_id)
                except Exception as exc:
                    log.exception("Job %s failed", job_id)
                    update_job(job_id, status="failed", stage="Error", error=str(exc)[:800])
                    # A partial live preview is intentionally retained for debugging.
                continue
        except Exception:
            log.exception("Worker database error")
        worker_wakeup.wait(timeout=6)
        worker_wakeup.clear()


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return jsonify(error=f"PDF too large (limit: {MAX_UPLOAD_MB} MiB)"), 413


@app.get("/")
def index():
    return render_template("index.html", limit=MAX_UPLOAD_MB)


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.get("/api/jobs")
def list_jobs():
    with db_conn() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 100").fetchall()
    return jsonify(jobs=[dict(row) for row in rows])


@app.get("/api/jobs/<job_id>")
def job_info(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.post("/api/jobs")
def create_job():
    uploaded = request.files.get("file")
    question = (request.form.get("question") or "Analyze the entire document").strip()
    if not uploaded or not uploaded.filename:
        return jsonify(error="Select a PDF file"), 400
    if not question or len(question) > MAX_QUESTION_CHARS:
        return jsonify(error=f"Instructions required (maximum {MAX_QUESTION_CHARS} characters)"), 400
    filename = uploaded.filename.replace("\\", "/").split("/")[-1][:120]
    if not filename.lower().endswith(".pdf"):
        return jsonify(error="Only .pdf files are accepted"), 400
    if uploaded.stream.read(5) != b"%PDF-":
        return jsonify(error="The file does not appear to be a valid PDF"), 400
    uploaded.stream.seek(0)
    job_id = uuid.uuid4().hex
    directory = job_dir(job_id)
    directory.mkdir(parents=True)
    try:
        uploaded.save(directory / "source.pdf")
        with db_conn() as con:
            con.execute("""INSERT INTO jobs (id, filename, question, status, stage, created_at, updated_at)
              VALUES (?, ?, ?, 'queued', 'Queued', ?, ?)""", (job_id, filename, question, utcnow(), utcnow()))
    except Exception:
        (directory / "source.pdf").unlink(missing_ok=True)
        directory.rmdir()
        raise
    worker_wakeup.set()
    return jsonify(id=job_id, status="queued"), 201


@app.get("/api/jobs/<job_id>/live")
def job_live(job_id):
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
    # A final snapshot may be from a completed/failed task: do not label it
    # as generating simply because it was active when last flushed.
    payload["active"] = bool(payload.get("active") and job["status"] == "running")
    payload["enabled"] = True
    return jsonify(payload)


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "failed":
        return jsonify(error="Only failed jobs can be retried"), 409
    update_job(job_id, status="queued", error="", stage="Retrying")
    worker_wakeup.set()
    return jsonify(status="queued")


@app.get("/api/jobs/<job_id>/report")
def report(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        abort(409)
    path = job_dir(job_id) / "rapport_final.md"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="text/markdown", as_attachment=True,
                     download_name="analysis_" + job_id[:8] + ".md")


if __name__ == "__main__":
    if not (1000 <= CHUNK_TOKENS <= 6000):
        raise SystemExit("CHUNK_TOKENS must be between 1000 and 6000")
    if CONTEXT_TOKENS < CHUNK_TOKENS + max(ANALYSIS_TOKENS, MERGE_TOKENS, FINAL_TOKENS) + 768:
        raise SystemExit("CONTEXT_TOKENS is too small for the configured chunk and output budgets")
    init_db()
    threading.Thread(target=worker_loop, name="pdf-worker", daemon=True).start()
    log.info("PDF processor listening on :%s, llama URL %s", APP_PORT, LLAMA_BASE)
    serve(app, host="0.0.0.0", port=APP_PORT, threads=4)
