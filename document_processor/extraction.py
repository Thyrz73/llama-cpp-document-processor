"""Turn a PDF into page-numbered text and split it into model-sized sections.

extract_pdf() reads every page with PyMuPDF, falls back to pypdf when the text
layer looks broken, and runs local Tesseract OCR only for pages that still lack
usable text. make_chunks() then groups the pages into sections that fit the
job's token budget, prefixing each passage with its "[Page N]" marker so that
citations can be checked later.

Limits come from the extraction settings saved with the job (keys: enabled,
languages, dpi, max_pixels, timeout, max_pages), so a retry always reuses them.
"""

import logging
import math
import re
import subprocess
import time
from typing import Any

import pymupdf
from pypdf import PdfReader

from .errors import ProcessingError
from .observability import current_job_id, event

# A page needs at least this many letters or digits to count as readable text.
MIN_USABLE_CHARS = 35


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


def make_chunks(pages, model, budget):
    """Group passages into sections of at most ``budget`` tokens.

    pages is a list of ``page``/``text`` dictionaries from extract_pdf().
    model must provide tokenize() to count tokens. Prefix each passage with
    its page reference, then group passages while the budget allows. Split
    passages that are still too long.

    Return ``text``/``pages``/``parts`` dictionaries; pages contains sorted unique
    physical page numbers, parts retains exact passage/page pairs independently
    of markers embedded in source text. Raise ProcessingError if a passage exceeds the
    budget. Propagate errors from the remote tokenizer.
    """
    units = []
    for p in pages:
        units.extend(split_with_page(p["text"], p["page"]))
    chunks = []
    current: list[dict[str, Any]] = []

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
                    raise ProcessingError("chunking_failed", "Unable to split a PDF passage into sufficiently small chunks.")
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
                chunks.append({"text": chunk_text(current), "pages": sorted(set(v["page"] for v in current)),
                               "parts": list(current)})
                current = [piece]
            else:
                current = candidate
    if current:
        chunks.append({"text": chunk_text(current), "pages": sorted(set(v["page"] for v in current)),
                       "parts": list(current)})
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


def ocr_page(page, number, settings):
    """Recognize a PyMuPDF page with local Tesseract.

    page must belong to an open document; number is used in error messages.
    Return cleaned text, or an empty string when OCR is disabled. Lower the
    rendering resolution to stay within ``settings["max_pixels"]`` before
    allocating the image, then run Tesseract with ``settings["languages"]``.

    Raise ProcessingError when the page exceeds the pixel budget or Tesseract is
    missing, fails, or exceeds ``settings["timeout"]``. Page rendering errors
    are propagated to the caller.
    """
    if not settings["enabled"]:
        return ""
    started = time.monotonic()
    job_id = current_job_id()
    event("ocr_started", job_id=job_id, page=number)
    # Reduce the render before allocation to limit memory use.
    pixels_at_dpi = (page.rect.width * settings["dpi"] / 72) * (page.rect.height * settings["dpi"] / 72)
    scale = min(1.0, math.sqrt(settings["max_pixels"] / max(1.0, pixels_at_dpi)))
    dpi = max(1, math.floor(settings["dpi"] * scale))
    if (page.rect.width * dpi / 72) * (page.rect.height * dpi / 72) > settings["max_pixels"]:
        raise ProcessingError("ocr_page_too_large", f"PDF page {number} exceeds the OCR pixel budget.")
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
        event("ocr_failed", logging.ERROR, job_id=job_id, page=number, error_code="ocr_missing")
        raise ProcessingError("ocr_missing", "Tesseract is missing from the container; rebuild the Docker image.") from exc
    except subprocess.TimeoutExpired:
        event("ocr_failed", logging.ERROR, job_id=job_id, page=number, error_code="ocr_timeout")
        raise ProcessingError("ocr_timeout", f"OCR timed out on page {number}.") from None
    finally:
        del png
    if process.returncode:
        event("ocr_failed", logging.ERROR, job_id=job_id, page=number, error_code="ocr_failed")
        raise ProcessingError("ocr_failed", f"OCR failed on page {number}. Check installed language packs.")
    try:
        text = process.stdout.decode("utf-8")
    except UnicodeDecodeError:
        event("ocr_failed", logging.ERROR, job_id=job_id, page=number, error_code="invalid_ocr_encoding")
        raise ProcessingError("invalid_ocr_encoding", "OCR returned text that is not valid UTF-8.") from None
    event("ocr_finished", job_id=job_id, page=number,
          duration_ms=round((time.monotonic() - started) * 1000, 1), characters=len(text))
    return normalize_extracted_text(text)


def extract_pdf(pdf_path, settings, progress=None):
    """Extract a PDF and report pages with insufficient text.

    pdf_path identifies the source file and settings holds the job's
    extraction limits. Try PyMuPDF, then pypdf when text is insufficient, and
    finally OCR when enabled. The optional progress callback receives a label
    during extraction and page OCR.

    Return a tuple containing page/text dictionaries, total page count,
    insufficient page numbers, and OCR page numbers. Keep non-empty text from
    pages judged insufficient.

    Raise ProcessingError when the PDF is unreadable, password-protected,
    exceeds ``settings["max_pages"]``, or contains no usable text. Propagate
    OCR errors; the document context closes it on exit.
    """
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        raise ProcessingError("unreadable_pdf", "Unable to read PDF.") from None
    with doc:
        if doc.needs_pass:
            raise ProcessingError("encrypted_pdf", "Encrypted PDF: remove the password before uploading.")
        count = len(doc)
        if count > settings["max_pages"]:
            raise ProcessingError("page_limit", f"PDF has {count} pages (configured maximum exceeded).")
        pages, missing, ocr_pages = [], [], []
        alt_reader = None
        for number in range(1, count + 1):
            if progress:
                progress(f"Extracting text: page {number}/{count}")
            page = doc[number - 1]
            try:
                text = normalize_extracted_text(page.get_text("text", sort=True))
            except Exception:
                event("extraction_fallback", logging.WARNING, page=number, reader="pymupdf",
                      job_id=current_job_id())
                text = ""
            if not text_is_usable(text):
                # Try another reader for unusual or incorrectly encoded fonts.
                try:
                    if alt_reader is None:
                        alt_reader = PdfReader(str(pdf_path), strict=False)
                    alternate = normalize_extracted_text(alt_reader.pages[number - 1].extract_text())
                    if text_is_usable(alternate) or len(alternate) > len(text):
                        text = alternate
                except Exception:
                    event("extraction_fallback", logging.WARNING, page=number, reader="pypdf",
                          job_id=current_job_id())
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
            event("extraction_empty", logging.WARNING, job_id=current_job_id(), pages=count)
            detail = "even after OCR" if settings["enabled"] else "(OCR disabled)"
            raise ProcessingError("no_usable_text", f"No usable text found in this PDF {detail}. Check the source PDF.")
        event("extraction_summary", job_id=current_job_id(), pages=count,
              ocr_pages=len(ocr_pages), insufficient_pages=len(missing), characters=sum(len(p["text"]) for p in pages))
        return pages, count, missing, ocr_pages
