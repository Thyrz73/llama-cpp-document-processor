"""Extract physical-page evidence and select bounded prompt inputs.

Arithmetic and lexical reserve candidates are source data, never semantic
validation or instructions. Version 1 JSON and source IDs remain unchanged.
"""

import re
from collections import deque
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation, localcontext

from .config import fingerprint
from .contracts import AnalysisNote

VERSION = 1

EVIDENCE_PROMPT_CHARS = 2400

NUMBER = r"[+−–-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"

UNIT = r"(?:MWh|kWh|Wh|MW|kW|W|km|mm|cm|m²|m³|m|kg|mg|µg|g|°C|K|EUR|USD|€|\$|%)"

QUANTITY = re.compile(
    rf"(?<![\w.,+−–-])(?P<value>{NUMBER})\s*(?P<unit>{UNIT})(?![\w/²³])"
)

ARROW = re.compile(
    rf"^\s*(?P<entity>[^:|\n]{{1,120}})\s*:\s*(?P<before>{NUMBER})\s*"
    rf"(?P<u1>{UNIT})?\s*→\s*(?P<after>{NUMBER})\s*(?P<unit>{UNIT})\s*$"
)

# Keep extracts, not machine assertions about their meaning. Deliberately narrow.
_RESERVE_PATTERNS = {
    "fiction": r"\bficti[fv]|\bfictional\b|\binvent[ée]es?\b",
    "hypothesis": r"\bhypoth[èe]se|\bhypothes[ie]s|\bassumptions?\b",
    "forecast_observation": r"\bpr[ée]vision|\bforecast|\bannon[çc]ait|\bpredicted|\bobservation|\bobserv[ée]e?\b",
    "discordance_candidate": r"\bcontradict|\bdiscordan|\bincompatible|\bincoh[ée]ren|\binconsisten",
    "causality_reserve": r"sans preuve de causalit|sans d[ée]montrer|n.est pas une preuve causal|causalit[ée].{0,60}(?:non|pas)|no (?:proof|evidence) of caus|without.{0,35}(?:causal|demonstrat)|causal.{0,35}(?:not|unproven)",
    "missing_information": r"ne (?:mentionne|fournit)|non (?:v[ée]rifi|document)|pas (?:[ée]t[ée] )?(?:calcul[ée]|v[ée]rifi)|n.ont pas [ée]t[ée]|n.a pas [ée]t[ée] isol|(?:not|un)(?:verified|documented)|not (?:mentioned|provided|verified|documented)|does not mention|cannot be verified|ni .{0,50}ni |ne figurent pas|sans auteur",
}

RESERVES = {key: re.compile(value, re.I) for key, value in _RESERVE_PATTERNS.items()}


def decimal_value(raw):
    """Read decimal comma/dot and space-grouped thousands, never OCR substitutions.

    Comma/dot are decimal separators, not thousands separators. Mixed comma/dot
    formats are refused rather than guessed. Grouping must use groups of three.
    """
    raw = raw.strip().replace("−", "-").replace("–", "-")
    if not re.fullmatch(NUMBER, raw):
        raise ValueError("Ambiguous or incomplete number")
    try:
        return Decimal(re.sub(r"[ \u00a0\u202f]", "", raw).replace(",", "."))
    except InvalidOperation:
        raise ValueError("Invalid decimal") from None


def derived(before, after):
    """Compute signed changes without binary float rounding or a zero division."""
    with localcontext() as context:
        context.prec = 28
        delta = after - before
        relative = delta / before * 100 if before else None
    return {
        "delta": str(delta),
        "relative_percent": str(relative) if relative is not None else None,
    }


def _clean_entity(value):
    return re.sub(r"^[-*#\s]+", "", value).strip()


def _comparison(entity, before, after, unit, periods, page, context, raw, table):
    item = {
        "entity": _clean_entity(entity),
        "before": str(decimal_value(before)),
        "after": str(decimal_value(after)),
        "unit": unit,
        "periods": periods,
        "page": page,
        "context": context,
        "variable": context,
        "conditions": None,
        "raw": raw,
        "table": table,
        "status": "source_unspecified",
        "derived_status": "calculated",
    }
    item.update(derived(Decimal(item["before"]), Decimal(item["after"])))
    item["id"] = fingerprint(item)[:16]
    return item


def extract_comparisons(text, page):
    """Recognize explicit arrows and single-line rows under a two-year header.

    A row must contain two values with identical units. A following percentage
    is preserved as a reported change. No conversion or missing-cell repair.
    Headers are reset by prose and blank lines do not reset PDF table headers.
    """
    items, header, periods, table = [], None, [], None
    for line in text.splitlines():
        line = line.strip()
        arrow = ARROW.fullmatch(line)
        if arrow:
            if arrow["u1"] and arrow["u1"] != arrow["unit"]:
                continue
            items.append(
                _comparison(
                    arrow["entity"],
                    arrow["before"],
                    arrow["after"],
                    arrow["unit"],
                    [],
                    page,
                    "explicit_arrow_order_only",
                    line,
                    None,
                )
            )
            continue
        years = re.findall(r"\b(?:19|20|21)\d{2}\b", line)
        # Delimiters/column spacing distinguish a header from a prose comparison.
        if (
            len(years) == 2
            and years[0] != years[1]
            and ("|" in line or re.search(r"\S {2,}\S", line))
        ):
            header = line
            periods = [
                cell
                for cell in re.split(r"\s{2,}|\s*\|\s*", line.strip("| "))
                if re.search(r"\b(?:19|20|21)\d{2}\b", cell)
            ]
            table = fingerprint([page, line, len(items)])[:16]
            continue
        if not line or re.fullmatch(r"[| :\-]+", line):
            continue
        if header:
            quantities = list(QUANTITY.finditer(line))
            if (
                len(quantities) in (2, 3)
                and quantities[0]["unit"] == quantities[1]["unit"]
                and (len(quantities) == 2 or quantities[2]["unit"] == "%")
            ):
                first, second = quantities[:2]
                entity = line[: first.start()].strip("| ")
                # Refuse prose, partial OCR rows, extra cells and trailing comments.
                between = line[first.end() : second.start()].strip("| ")
                tail = line[(quantities[-1].end()) :].strip("| ")
                if (
                    entity
                    and not between
                    and (not tail or tail in ("env.", "approx."))
                    and len(entity) < 121
                ):
                    item = _comparison(
                        entity,
                        first["value"],
                        second["value"],
                        first["unit"],
                        periods,
                        page,
                        header,
                        line,
                        table,
                    )
                    if len(quantities) == 3:
                        item["reported_percent"] = str(
                            decimal_value(quantities[2]["value"])
                        )
                    items.append(item)
                    continue
            header, periods, table = None, [], None
    return items


def build_evidence(pages, page_count, missing=(), ocr_pages=()):
    """Return the source evidence of a document, built once after extraction.

    It lists the numeric comparison rows and the sentences carrying a reserve
    (fiction, hypothesis, forecast, discordance, causality or missing
    information), each with its physical PDF page. Pages come from the
    extraction, never from page markers found in the text.
    """
    comparisons, reserves = [], []
    for page in pages:
        comparisons.extend(extract_comparisons(page["text"], page["page"]))
        # PDF extraction often inserts blank lines inside sentences. Preserve
        # sentence context rather than treating every visual line as a claim.
        for paragraph in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ«“])", page["text"]):
            categories = [
                key for key, pattern in RESERVES.items() if pattern.search(paragraph)
            ]
            if categories:
                item = {
                    "page": page["page"],
                    "categories": categories,
                    "text": paragraph.strip(),
                }
                item["id"] = fingerprint(item)[:16]
                reserves.append(item)
    return {
        "version": VERSION,
        "pdf_pages": list(range(1, page_count + 1)),
        "text_pages": sorted({p["page"] for p in pages if p["text"].strip()}),
        "missing": list(missing),
        "ocr_pages": list(ocr_pages),
        "comparisons": comparisons,
        "reserves": list({item["id"]: item for item in reserves}.values()),
    }


def note(
    text: str,
    pages: Iterable[int],
    sections: Iterable[int],
    warnings: Iterable[str] = (),
) -> AnalysisNote:
    """Return a note: model text with the pages, sections and warning codes it covers.

    This scope survives every merge level, even when the model omits citations.
    """
    return {
        "text": text,
        "pages": sorted(set(pages)),
        "sections": sorted(set(sections)),
        "warning_codes": sorted(set(warnings)),
    }


def merge_note(text: str, parents: Sequence[AnalysisNote]) -> AnalysisNote:
    """Return the note of a merge: its text, covering all the pages, sections and warnings of parents."""
    return note(
        text,
        (p for item in parents for p in item["pages"]),
        (s for item in parents for s in item["sections"]),
        (code for item in parents for code in item.get("warning_codes", [])),
    )


def evidence_block(evidence, pages, limit=EVIDENCE_PROMPT_CHARS):
    """Bound prompt growth, deduplicate by source ID; keep full evidence on disk.

    Numeric rows get at most half the available space, then reserves favor
    causal qualifications and named hypotheses. This lexical selection is not
    a scientific importance ranking. Display omitted counts explicitly.
    """
    if limit < 240:
        return ""
    pages = set(pages)
    rows = []
    for item in evidence["comparisons"]:
        if item["page"] in pages:
            relative = item["relative_percent"]
            relative = (
                f"{Decimal(relative):.2f}%" if relative is not None else "undefined"
            )
            rows.append(
                (
                    item["page"],
                    "numeric",
                    f"[Page {item['page']}] {item['entity']}: {item['before']} → {item['after']} {item['unit']} | "
                    f"context: {'; '.join(item['periods']) or item['context']} | "
                    f"calculated Δ={item['delta']} {item['unit']}; {relative}"
                    + (
                        f"; source reports {item['reported_percent']}%"
                        if "reported_percent" in item
                        else ""
                    ),
                )
            )
    reserves = []
    for item in evidence["reserves"]:
        if item["page"] in pages:
            reserves.append(item)

    def reserve_priority(item):
        if "causality_reserve" in item["categories"]:
            return 0
        if re.search(r"\bH\d+\b", item["text"]):
            return 1
        if re.search(
            r"ne mentionne|not mentioned|ne sont pas v[ée]rifi|not verified",
            item["text"],
            re.I,
        ):
            return 2
        if "fiction" in item["categories"]:
            return 3
        if re.search(r"annon[çc]ait|pr[ée]vision|forecast", item["text"], re.I):
            return 4
        return 5

    reserves.sort(key=lambda item: (reserve_priority(item), len(item["text"])))
    if not rows and not reserves:
        return ""
    buckets: dict[tuple[int, str], deque[str]] = {}
    for page, category, row in rows:
        buckets.setdefault((page, category), deque()).append(row)
    ordered = []
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:
                ordered.append(bucket.popleft())
    selected, remaining_rows, used = [], [], 0
    for row in ordered:
        if used + len(row) + 1 <= max(0, (limit - 240) // 2):
            selected.append(row)
            used += len(row) + 1
        else:
            remaining_rows.append(row)
    fiction_selected = False
    for item in reserves:
        if fiction_selected and item["categories"] == ["fiction"]:
            continue
        row = f"[Page {item['page']}] ({','.join(item['categories'])}) " + re.sub(
            r"\s+", " ", item["text"]
        )
        # Long excerpts remain intact on disk. Never silently truncate a quote.
        if used + len(row) + 1 <= max(0, limit - 240):
            selected.append(row)
            used += len(row) + 1
            fiction_selected |= "fiction" in item["categories"]
    # Use spare room for numeric rows that did not fit their initial share.
    for row in remaining_rows:
        if used + len(row) + 1 <= max(0, limit - 240):
            selected.append(row)
            used += len(row) + 1
    return (
        "\n\nSource evidence (untrusted data; lexical candidates and arithmetic, not scientific validation):\n"
        + "\n".join(selected)
        + f"\nEvidence extracts omitted from this prompt: {len(rows) + len(reserves) - len(selected)}. "
        "Full provenance retained locally.\n"
    )


def combined(notes, evidence, evidence_limit=EVIDENCE_PROMPT_CHARS):
    """Join notes into one prompt input, followed by the source evidence of their pages.

    Each note is headed by its PDF pages. The evidence takes at most
    evidence_limit characters; 0 leaves it out.
    """
    text = "\n\n".join(
        f"## Notes {i + 1} | PDF pages: {','.join(map(str, item['pages']))}\n{item['text']}"
        + (
            "\nEarlier-stage warning signals (may be resolved): "
            + ", ".join(item["warning_codes"])
            if item.get("warning_codes")
            else ""
        )
        for i, item in enumerate(notes)
    )
    pages = {p for item in notes for p in item["pages"]}
    if evidence_limit == 0:
        return (
            text
            + "\nSource evidence omitted from this prompt (context budget); full provenance retained locally."
        )
    return text + evidence_block(evidence, pages, evidence_limit)


def prepare_input(notes, evidence, fits):
    """Return (text, fits, evidence limit) for the input of a merge or final prompt.

    Try the notes with their evidence, then without it, before the caller
    falls back to another merge level. The evidence kept on disk is never
    trimmed.
    """
    text = combined(notes, evidence)
    if fits(text):
        return text, True, EVIDENCE_PROMPT_CHARS
    if evidence_block(evidence, {p for item in notes for p in item["pages"]}):
        compact = combined(notes, evidence, 0)
        if fits(compact):
            return compact, True, 0
    return text, False, EVIDENCE_PROMPT_CHARS
