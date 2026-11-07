"""Conservative mechanical warnings: citations, numbers, quotations and wording.

These checks never rewrite generated text or establish scientific/causal
validity. Prepared normalization is attempt-local and never persisted.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from .evidence import QUANTITY, VERSION, decimal_value

CITATION = re.compile(r"\[(?:pages?)\s+([^\]\n]*)\]", re.I)
# Exact extraction.make_chunks() marker, independent of printed page numbering.
PAGE_REFERENCE = re.compile(r"\[Page (\d+)\]")


def compare_quantities(left, right):
    """Compare explicitly structured quantities only within the same scope.

    A forecast gap is not a contradiction. Two disagreeing observations are a
    numerical discordance to investigate, not proof of scientific contradiction.
    """
    keys = ("entity", "variable", "unit", "period", "conditions")
    if any(not left.get(key) or left.get(key) != right.get(key) for key in keys):
        return {"kind": "not_comparable", "checked": False}
    try:
        gap = decimal_value(right["value"]) - decimal_value(left["value"])
    except (KeyError, ValueError):
        return {"kind": "insufficient_data", "checked": False}
    statuses = {left.get("status"), right.get("status")}
    if statuses == {"forecast", "observation"}:
        kind = "forecast_observation_gap"
    elif statuses == {"observation"}:
        kind = "agreement" if gap == 0 else "numerical_discordance"
    else:
        return {"kind": "status_not_comparable", "checked": False}
    return {"kind": kind, "checked": True, "difference": str(gap)}


def _citation_parts(body):
    parts = re.split(r"\s*(?:,|;|&|\bet\b|\band\b)\s*", body, flags=re.I)
    result = []
    for part in parts:
        match = re.fullmatch(r"(\d{1,9})(?:\s*[-–—]\s*(\d{1,9}))?", part.strip())
        if not match:
            return None
        start, end = int(match[1]), int(match[2] or match[1])
        if end < start:
            return None
        result.append((start, end))
    return result


def citation_checks(text, pdf_pages, input_pages, text_pages):
    """Check physical page membership, input scope and extract availability.

    Test intervals against the page set without expanding untrusted huge ranges.
    Printed page labels and markers embedded in a PDF are never authoritative.
    """
    pdf_pages, input_pages, text_pages = map(set, (pdf_pages, input_pages, text_pages))
    issues, checked = [], 0
    for match in CITATION.finditer(text):
        parts = _citation_parts(match[1])
        if parts is None:
            issues.append({"code": "malformed_page_reference", "reference": match[0]})
            continue
        for start, end in parts:
            checked += 1
            existing = {p for p in pdf_pages if start <= p <= end}
            if len(existing) != end - start + 1:
                issues.append({"code": "nonexistent_page", "reference": match[0]})
            if existing - input_pages:
                issues.append({"code": "page_outside_input", "reference": match[0]})
            if existing - text_pages:
                issues.append({"code": "page_without_text", "reference": match[0]})
    return {
        "checked_intervals": checked,
        "semantic_validation": False,
        "issues": issues,
    }


def _normalized(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


@dataclass(frozen=True)
class PreparedEvidence:
    """Attempt-local normalization of an unchanged evidence batch; never persisted."""

    source: dict
    version: int
    entities: tuple[str, ...]
    quotes: tuple[tuple[int, str], ...]


def prepare_evidence(evidence, prepared=None):
    """Prepare NFKC source strings once, retaining original rows and warning order."""
    if (
        prepared is not None
        and prepared.source is evidence
        and prepared.version == VERSION
    ):
        return prepared
    items = [(item["page"], item["text"]) for item in evidence["reserves"]]
    items += [(item["page"], item["raw"]) for item in evidence["comparisons"]]
    return PreparedEvidence(
        evidence,
        VERSION,
        tuple(_normalized(item["entity"]) for item in evidence["comparisons"]),
        tuple((page, _normalized(text)) for page, text in items),
    )


def _entity_in_line(entity, line, normalized=None, candidate=None):
    # Exact label, or an explicitly extracted leading code: A · School / A — School.
    normalized = _normalized(entity) if normalized is None else normalized
    candidate = _normalized(line) if candidate is None else candidate
    if normalized and re.search(
        r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", candidate
    ):
        return True
    code = re.match(r"^([A-Za-z0-9]{1,8})\s*[·—–-]\s*\S", entity)
    return bool(
        code
        and re.search(
            r"(?:^\s*[-*]?\s*"
            + re.escape(code[1])
            + r"\s*:|\("
            + re.escape(code[1])
            + r"\))",
            line,
        )
    )


def numeric_checks(answer, evidence, input_pages, *, prepared=None):
    """Flag only uniquely scoped, explicitly signed/labelled delta claims.

    Multiple records for an entity require exact periods/context in the claim,
    otherwise abstain. A possible mismatch is never an automatic correction.
    """
    prepared = prepare_evidence(evidence, prepared)
    issues, checked, skipped = [], 0, 0
    for line in answer.splitlines():
        quantities = list(QUANTITY.finditer(line))
        if not quantities:
            continue
        normalized_line = _normalized(line)
        candidates = [
            item
            for item, entity in zip(
                evidence["comparisons"], prepared.entities, strict=False
            )
            if item["page"] in input_pages
            and _entity_in_line(item["entity"], line, entity, normalized_line)
        ]
        refs = [int(m[1]) for m in PAGE_REFERENCE.finditer(line)]
        if refs:
            candidates = [item for item in candidates if item["page"] in refs]
        # Duplicate source rows from repeat headers must not resolve ambiguity.
        if len(candidates) != 1:
            skipped += 1
            continue
        item = candidates[0]
        claim_years = set(re.findall(r"\b(?:19|20|21)\d{2}\b", CITATION.sub("", line)))
        source_years = set(re.findall(r"\b(?:19|20|21)\d{2}\b", item["context"]))
        if claim_years and not claim_years <= source_years:
            skipped += 1
            continue
        if item["unit"] == "%":
            # Percent change versus percentage points cannot be inferred here.
            skipped += 1
            continue
        # Only one absolute quantity (+ optional relative percentage) can be a
        # simple delta claim. Rows containing before/after values are not deltas.
        absolute = [q for q in quantities if q["unit"] != "%"]
        if len(absolute) > 1:
            skipped += 1
            continue
        labelled_change = bool(
            re.search(r"\b(?:delta|variation|change|évolution)\b|Δ", line, re.I)
        )
        relative_summary = any(
            q["unit"] == "%" and q["value"].startswith(("+", "-", "−", "–"))
            for q in quantities
        )
        for quantity in quantities:
            signed = quantity["value"].startswith(("+", "-", "−", "–"))
            if not labelled_change and (
                not signed or (quantity["unit"] != "%" and not relative_summary)
            ):
                skipped += 1
                continue
            unit = quantity["unit"]
            if unit not in (item["unit"], "%"):
                skipped += 1
                continue
            expected = item["relative_percent"] if unit == "%" else item["delta"]
            if expected is None:
                skipped += 1
                continue
            actual = decimal_value(quantity["value"])
            # Half a displayed decimal place: accept legitimate rounding only.
            tolerance = Decimal(1).scaleb(actual.as_tuple().exponent) / 2
            checked += 1
            if abs(actual - Decimal(expected)) > tolerance:
                issues.append(
                    {
                        "code": "numeric_mismatch",
                        "evidence_id": item["id"],
                        "entity": item["entity"],
                        "page": item["page"],
                        "unit": unit,
                        "expected": expected,
                        "actual": str(actual),
                    }
                )
    return {
        "checked": checked,
        "unchecked_lines_or_values": skipped,
        "issues": issues,
        "status": "partial" if checked else "not_performed",
        "scope": "unique_entity_explicit_delta_only",
    }


def certainty_checks(answer, evidence, input_pages):
    """Lexical escalation signals only, never a causal/semantic verdict.

    Reserves stay available independently of generated notes. A warning does
    not prove two sentences concern the same intervention or protocol.
    """
    reserves = [item for item in evidence["reserves"] if item["page"] in input_pages]
    causal = [item for item in reserves if "causality_reserve" in item["categories"]]
    missing = [
        item
        for item in reserves
        if "missing_information" in item["categories"]
        and re.search(r"étalonnage|calibrat", item["text"], re.I)
    ]
    issues = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", answer):
        if (
            causal
            and re.search(
                r"a (?:entraîné|causé|permis)|\bcaused\b|\bled to\b", sentence, re.I
            )
            and not re.search(
                r"\b(?:pas|non|sans|pourrait|peut|not|no|may|might|could|without)\b",
                sentence,
                re.I,
            )
        ):
            issues.append(
                {
                    "code": "certainty_escalation_candidate",
                    "evidence_ids": [item["id"] for item in causal],
                    "detection": "lexical_only",
                }
            )
        if missing and re.search(
            r"aucun étalonnage.{0,35}(?:réalisé|effectué)|no calibration.{0,25}performed|"
            r"étalonnage n.a (?:pas|jamais) été (?:réalisé|effectué)",
            sentence,
            re.I,
        ):
            issues.append(
                {
                    "code": "missing_information_escalation_candidate",
                    "evidence_ids": [item["id"] for item in missing],
                    "detection": "lexical_only",
                }
            )
    return issues


def quote_checks(answer, evidence, input_pages, *, prepared=None):
    """Locate exact reserve/row quotations only; this does not validate a claim."""
    prepared = prepare_evidence(evidence, prepared)
    issues = []
    for line in answer.splitlines():
        refs = {int(m[1]) for m in PAGE_REFERENCE.finditer(line)}
        if not refs:
            continue
        for match in re.finditer(r'«([^»]+)»|"([^"\n]+)"|“([^”]+)”', line):
            quote = _normalized(
                next(part for part in match.groups() if part is not None)
            )
            if len(quote) < 30:
                continue
            source_pages = {page for page, text in prepared.quotes if quote in text}
            # A line can cite several claims; abstain if it includes the quote's source.
            if (
                len(source_pages) == 1
                and source_pages <= set(input_pages)
                and refs.isdisjoint(source_pages)
            ):
                issues.append(
                    {
                        "code": "quote_page_mismatch",
                        "reference": ", ".join(f"[Page {p}]" for p in sorted(refs)),
                        "matching_pages": sorted(source_pages),
                        "detection": "exact_text_only",
                    }
                )
    return issues


def audit(answer, evidence, input_pages, *, prepared=None):
    """Check one generated text against the source evidence.

    input_pages are the PDF pages this step received. Return the citation and
    numeric check details; "issues" lists every warning, including certainty
    wording and quotations found on another page.
    """
    prepared = prepare_evidence(evidence, prepared)
    citations = citation_checks(
        answer,
        evidence["pdf_pages"],
        input_pages,
        set(evidence["text_pages"]) - set(evidence["missing"]),
    )
    numeric = numeric_checks(answer, evidence, set(input_pages), prepared=prepared)
    return {
        "version": VERSION,
        "citations": citations,
        "numeric": numeric,
        "issues": citations["issues"]
        + numeric["issues"]
        + certainty_checks(answer, evidence, input_pages)
        + quote_checks(answer, evidence, input_pages, prepared=prepared),
        "semantic_validation": False,
    }
