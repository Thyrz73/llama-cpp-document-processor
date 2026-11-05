"""Turn a PDF into page-numbered text and split it into model-sized sections.

extract_pdf() reads every page with PyMuPDF, falls back to pypdf when the text
layer looks broken, and runs local Tesseract OCR only for pages that still lack
usable text. make_chunks() then groups the pages into sections that fit the
job's token budget, prefixing each passage with its "[Page N]" marker so that
citations can be checked later.

Limits come from the extraction settings saved with the job (keys: enabled,
languages, dpi, max_pixels, timeout, max_pages), so a retry always reuses them.
"""

import hashlib
import logging
import math
import re
import subprocess
import time
from collections import OrderedDict, deque
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
        if (
            line_start >= limit * 0.6
            and re.match(r"^(#{1,6}\s|\d+(?:\.\d+)*[.)]?\s+[A-Z])", last_line)
            and len(last_line) < 120
        ):
            end = line_start
        out.append({"page": page, "text": text[:end].strip()})
        text = text[end:].strip()
    return [v for v in out if v["text"]]


def make_chunks(pages, model, budget):
    """Group passages into sections of at most ``budget`` tokens.

    pages is a list of ``page``/``text`` dictionaries from extract_pdf().
    model must provide tokenize() to count tokens. Memoize exact counts and
    estimate additions with separator/safety headroom. This is only a scheduling
    heuristic: tokenization is not additive, and every emitted section is checked
    exactly. Oversized groups are bisected at passage boundaries until they fit.

    Return ``text``/``pages``/``parts`` dictionaries; pages contains sorted unique
    physical page numbers, parts retains lossless passage/page pairs independently
    of markers embedded in source text. Raise chunking_failed for invalid counts,
    invalid budgets or an indivisible passage that cannot fit. Propagate tokenizer
    failures and cancellation unchanged.
    """

    def failed():
        return ProcessingError(
            "chunking_failed",
            "Unable to split a PDF passage into sufficiently small chunks.",
        )

    if type(budget) is not int or budget < 1:
        raise failed()

    units = []
    for p in pages:
        # Reuse the natural cuts, restoring whitespace stripped by split_with_page().
        text, start = p["text"], 0
        fragments = split_with_page(text, p["page"])
        for index, fragment in enumerate(fragments):
            end = text.find(fragment["text"], start) + len(fragment["text"])
            if index == len(fragments) - 1:
                end = len(text)
            units.append({"page": p["page"], "text": text[start:end]})
            start = end
        if not fragments and text:
            units.append({"page": p["page"], "text": text})
    chunks = []
    current: list[dict[str, Any]] = []
    estimated = 0
    counts: OrderedDict[bytes, int] = OrderedDict()

    def count(text):
        """Keep bounded count-only memoization; cache hits remain cancellable."""
        key = hashlib.sha256(text.encode("utf-8")).digest()
        if key in counts:
            check = getattr(model, "check_cancelled", None)
            if check is not None:
                check()
            counts.move_to_end(key)
            return counts[key]
        value = model.tokenize(text)
        if type(value) is not int or value < 0:
            raise failed()
        counts[key] = value
        if len(counts) > 1024:
            counts.popitem(last=False)
        return value

    def chunk_text(parts):
        """Join parts with page markers."""
        return "\n\n".join(f"[Page {x['page']}]\n{x['text']}" for x in parts)

    def emit(parts):
        """Exact validation only; each failed group becomes two strictly smaller groups."""
        pending_groups = [parts]
        while pending_groups:
            group = pending_groups.pop()
            text = chunk_text(group)
            if count(text) <= budget:
                chunks.append(
                    {
                        "text": text,
                        "pages": sorted({part["page"] for part in group}),
                        "parts": list(group),
                    }
                )
            elif len(group) > 1:
                middle = len(group) // 2
                pending_groups.extend((group[middle:], group[:middle]))
            else:
                raise failed()  # Every individual passage was already checked.

    pending = deque(units)
    while pending:
        piece = pending.popleft()
        solo_tokens = count(chunk_text([piece]))
        if solo_tokens > budget:
            size = len(piece["text"])
            if size < 2:
                raise failed()
            half = size // 2
            radius = min(300, size // 5)
            pivot = half
            for boundary in ("\n\n", "\n", " "):
                found = piece["text"].rfind(
                    boundary, max(1, half - radius), half + radius
                )
                cut = found + len(boundary)
                if found >= 0 and 0 < cut < size:
                    pivot = cut
                    break
            pending.appendleft({"page": piece["page"], "text": piece["text"][pivot:]})
            pending.appendleft({"page": piece["page"], "text": piece["text"][:pivot]})
            continue
        # Reserve at least eight tokens per join, or 5% for larger passages.
        # This is not a mathematical upper bound for an arbitrary tokenizer.
        addition = solo_tokens + max(8, (solo_tokens + 19) // 20)
        if current and estimated + addition > budget:
            candidate = current + [piece]
            exact = count(chunk_text(candidate))
            if exact > budget:
                emit(current)
                current, estimated = [piece], solo_tokens
            else:
                current, estimated = candidate, exact
        else:
            current.append(piece)
            estimated = estimated + addition if len(current) > 1 else solo_tokens
    if current:
        emit(current)
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
    pixels_at_dpi = (page.rect.width * settings["dpi"] / 72) * (
        page.rect.height * settings["dpi"] / 72
    )
    scale = min(1.0, math.sqrt(settings["max_pixels"] / max(1.0, pixels_at_dpi)))
    dpi = max(1, math.floor(settings["dpi"] * scale))
    if (page.rect.width * dpi / 72) * (page.rect.height * dpi / 72) > settings[
        "max_pixels"
    ]:
        raise ProcessingError(
            "ocr_page_too_large", f"PDF page {number} exceeds the OCR pixel budget."
        )
    # Encode the bitmap right away: only the smaller PNG stays in memory while Tesseract runs.
    png = page.get_pixmap(
        dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False, annots=False
    ).tobytes("png")
    try:
        process = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", settings["languages"], "--psm", "3"],
            input=png,
            capture_output=True,
            timeout=settings["timeout"],
            check=False,
        )
    except FileNotFoundError as exc:
        event(
            "ocr_failed",
            logging.ERROR,
            job_id=job_id,
            page=number,
            error_code="ocr_missing",
        )
        raise ProcessingError(
            "ocr_missing",
            "Tesseract is missing from the container; rebuild the Docker image.",
        ) from exc
    except subprocess.TimeoutExpired:
        event(
            "ocr_failed",
            logging.ERROR,
            job_id=job_id,
            page=number,
            error_code="ocr_timeout",
        )
        raise ProcessingError(
            "ocr_timeout", f"OCR timed out on page {number}."
        ) from None
    if process.returncode:
        event(
            "ocr_failed",
            logging.ERROR,
            job_id=job_id,
            page=number,
            error_code="ocr_failed",
        )
        raise ProcessingError(
            "ocr_failed",
            f"OCR failed on page {number}. Check installed language packs.",
        )
    try:
        text = process.stdout.decode("utf-8")
    except UnicodeDecodeError:
        event(
            "ocr_failed",
            logging.ERROR,
            job_id=job_id,
            page=number,
            error_code="invalid_ocr_encoding",
        )
        raise ProcessingError(
            "invalid_ocr_encoding", "OCR returned text that is not valid UTF-8."
        ) from None
    event(
        "ocr_finished",
        job_id=job_id,
        page=number,
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        characters=len(text),
    )
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
            raise ProcessingError(
                "encrypted_pdf", "Encrypted PDF: remove the password before uploading."
            )
        count = len(doc)
        if count > settings["max_pages"]:
            raise ProcessingError(
                "page_limit", f"PDF has {count} pages (configured maximum exceeded)."
            )
        pages, missing, ocr_pages = [], [], []
        alt_reader = None
        for number in range(1, count + 1):
            if progress:
                progress(f"Extracting text: page {number}/{count}")
            page = doc[number - 1]
            try:
                text = normalize_extracted_text(page.get_text("text", sort=True))
            except Exception:
                event(
                    "extraction_fallback",
                    logging.WARNING,
                    page=number,
                    reader="pymupdf",
                    job_id=current_job_id(),
                )
                text = ""
            if not text_is_usable(text):
                # Try another reader for unusual or incorrectly encoded fonts.
                try:
                    if alt_reader is None:
                        alt_reader = PdfReader(str(pdf_path), strict=False)
                    alternate = normalize_extracted_text(
                        alt_reader.pages[number - 1].extract_text()
                    )
                    if text_is_usable(alternate) or len(alternate) > len(text):
                        text = alternate
                except Exception:
                    event(
                        "extraction_fallback",
                        logging.WARNING,
                        page=number,
                        reader="pypdf",
                        job_id=current_job_id(),
                    )
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
            # Release the page now: rebinding `page` would only free it after loading the next one.
            del page
        if not any(text_is_usable(p["text"]) for p in pages):
            event(
                "extraction_empty",
                logging.WARNING,
                job_id=current_job_id(),
                pages=count,
            )
            detail = "even after OCR" if settings["enabled"] else "(OCR disabled)"
            raise ProcessingError(
                "no_usable_text",
                f"No usable text found in this PDF {detail}. Check the source PDF.",
            )
        event(
            "extraction_summary",
            job_id=current_job_id(),
            pages=count,
            ocr_pages=len(ocr_pages),
            insufficient_pages=len(missing),
            characters=sum(len(p["text"]) for p in pages),
        )
        return pages, count, missing, ocr_pages
