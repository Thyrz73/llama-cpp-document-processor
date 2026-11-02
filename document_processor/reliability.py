"""Cheap, conservative checks; source evidence is private data, never instructions.

No semantic citation validation is attempted. Numeric extraction accepts only
explicit two-value comparisons (arrows or a two-period table with units). All
other layouts remain unchecked. Nothing here rewrites model output.
"""

from collections import deque
from decimal import Decimal, InvalidOperation, localcontext
import re
import unicodedata

from .config import fingerprint

VERSION = 1
EVIDENCE_PROMPT_CHARS = 2400
NUMBER = r"[+−–-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"
UNIT = r"(?:MWh|kWh|Wh|MW|kW|W|km|mm|cm|m²|m³|m|kg|mg|µg|g|°C|K|EUR|USD|€|\$|%)"
QUANTITY = re.compile(rf"(?<![\w.,+−–-])(?P<value>{NUMBER})\s*(?P<unit>{UNIT})(?![\w/²³])")
ARROW = re.compile(rf"^\s*(?P<entity>[^:|\n]{{1,120}})\s*:\s*(?P<before>{NUMBER})\s*"
                   rf"(?P<u1>{UNIT})?\s*→\s*(?P<after>{NUMBER})\s*(?P<unit>{UNIT})\s*$")
CITATION = re.compile(r"\[(?:pages?)\s+([^\]\n]*)\]", re.I)
# Keep extracts, not machine assertions about their meaning. Deliberately narrow.
RESERVES = {
    "fiction": r"\bficti[fv]|\bfictional\b|\binvent[ée]es?\b",
    "hypothesis": r"\bhypoth[èe]se|\bhypothes[ie]s|\bassumptions?\b",
    "forecast_observation": r"\bpr[ée]vision|\bforecast|\bannon[çc]ait|\bpredicted|\bobservation|\bobserv[ée]e?\b",
    "discordance_candidate": r"\bcontradict|\bdiscordan|\bincompatible|\bincoh[ée]ren|\binconsisten",
    "causality_reserve": r"sans preuve de causalit|sans d[ée]montrer|n.est pas une preuve causal|causalit[ée].{0,60}(?:non|pas)|no (?:proof|evidence) of caus|without.{0,35}(?:causal|demonstrat)|causal.{0,35}(?:not|unproven)",
    "missing_information": r"ne (?:mentionne|fournit)|non (?:v[ée]rifi|document)|pas (?:[ée]t[ée] )?(?:calcul[ée]|v[ée]rifi)|n.ont pas [ée]t[ée]|n.a pas [ée]t[ée] isol|(?:not|un)(?:verified|documented)|not (?:mentioned|provided|verified|documented)|does not mention|cannot be verified|ni .{0,50}ni |ne figurent pas|sans auteur",
}
RESERVES = {key: re.compile(value, re.I) for key, value in RESERVES.items()}


def decimal_value(raw):
    """Read decimal comma/dot and space-grouped thousands, never OCR substitutions.

    Comma/dot are decimal separators, not thousands separators. Mixed comma/dot
    formats are refused rather than guessed. Grouping must use groups of three.
    """
    raw = raw.strip().replace('−', '-').replace('–', '-')
    if not re.fullmatch(NUMBER, raw):
        raise ValueError("Ambiguous or incomplete number")
    try:
        return Decimal(re.sub(r"[ \u00a0\u202f]", "", raw).replace(',', '.'))
    except InvalidOperation:
        raise ValueError("Invalid decimal") from None


def derived(before, after):
    """Compute signed changes without binary float rounding or a zero division."""
    with localcontext() as context:
        context.prec = 28
        delta = after - before
        relative = delta / before * 100 if before else None
    return {"delta": str(delta), "relative_percent": str(relative) if relative is not None else None}


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
    return {"checked_intervals": checked, "semantic_validation": False, "issues": issues}


def _clean_entity(value):
    return re.sub(r"^[-*#\s]+", "", value).strip()


def _comparison(entity, before, after, unit, periods, page, context, raw, table):
    item = {"entity": _clean_entity(entity), "before": str(decimal_value(before)),
            "after": str(decimal_value(after)), "unit": unit, "periods": periods,
            "page": page, "context": context, "variable": context, "conditions": None,
            "raw": raw, "table": table,
            "status": "source_unspecified", "derived_status": "calculated"}
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
            if arrow['u1'] and arrow['u1'] != arrow['unit']:
                continue
            items.append(_comparison(arrow['entity'], arrow['before'], arrow['after'], arrow['unit'],
                                     [], page, "explicit_arrow_order_only", line, None))
            continue
        years = re.findall(r"\b(?:19|20|21)\d{2}\b", line)
        # Delimiters/column spacing distinguish a header from a prose comparison.
        if len(years) == 2 and years[0] != years[1] and ('|' in line or re.search(r"\S {2,}\S", line)):
            header = line
            periods = [cell for cell in re.split(r"\s{2,}|\s*\|\s*", line.strip('| '))
                       if re.search(r"\b(?:19|20|21)\d{2}\b", cell)]
            table = fingerprint([page, line, len(items)])[:16]
            continue
        if not line or re.fullmatch(r"[| :\-]+", line):
            continue
        if header:
            quantities = list(QUANTITY.finditer(line))
            if len(quantities) in (2, 3) and quantities[0]['unit'] == quantities[1]['unit'] and (
                    len(quantities) == 2 or quantities[2]['unit'] == '%'):
                first, second = quantities[:2]
                entity = line[:first.start()].strip('| ')
                # Refuse prose, partial OCR rows, extra cells and trailing comments.
                between = line[first.end():second.start()].strip('| ')
                tail = line[(quantities[-1].end()):].strip('| ')
                if entity and not between and (not tail or tail in ('env.', 'approx.')) and len(entity) < 121:
                    item = _comparison(entity, first['value'], second['value'], first['unit'],
                                       periods, page, header, line, table)
                    if len(quantities) == 3:
                        item['reported_percent'] = str(decimal_value(quantities[2]['value']))
                    items.append(item)
                    continue
            header, periods, table = None, [], None
    return items


def build_evidence(pages, page_count, missing=(), ocr_pages=()):
    """Build once from physical extraction metadata; never infer pages from text."""
    comparisons, reserves = [], []
    for page in pages:
        comparisons.extend(extract_comparisons(page['text'], page['page']))
        # PDF extraction often inserts blank lines inside sentences. Preserve
        # sentence context rather than treating every visual line as a claim.
        for paragraph in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ«“])", page['text']):
            categories = [key for key, pattern in RESERVES.items() if pattern.search(paragraph)]
            if categories:
                item = {"page": page['page'], "categories": categories, "text": paragraph.strip()}
                item['id'] = fingerprint(item)[:16]
                reserves.append(item)
    return {"version": VERSION, "pdf_pages": list(range(1, page_count + 1)),
            "text_pages": sorted({p['page'] for p in pages if p['text'].strip()}),
            "missing": list(missing), "ocr_pages": list(ocr_pages),
            "comparisons": comparisons,
            "reserves": list({item['id']: item for item in reserves}.values())}


def note(text, pages, sections, warnings=()):
    """A model may omit citations; metadata still survives all merge levels."""
    return {"text": text, "pages": sorted(set(pages)), "sections": sorted(set(sections)),
            "warning_codes": sorted(set(warnings))}


def merge_note(text, parents):
    return note(text, (p for item in parents for p in item['pages']),
                (s for item in parents for s in item['sections']),
                (code for item in parents for code in item.get('warning_codes', [])))


def evidence_block(evidence, pages, limit=EVIDENCE_PROMPT_CHARS):
    """Bound prompt growth, deduplicate by source ID; keep full evidence on disk.

    Numeric rows get at most half the available space, then reserves favor
    causal qualifications and named hypotheses. This lexical selection is not
    a scientific importance ranking. Display omitted counts explicitly.
    """
    if limit < 240:
        return ''
    pages = set(pages)
    rows = []
    for item in evidence['comparisons']:
        if item['page'] in pages:
            relative = item['relative_percent']
            relative = f"{Decimal(relative):.2f}%" if relative is not None else 'undefined'
            rows.append((item['page'], 'numeric',
                         f"[Page {item['page']}] {item['entity']}: {item['before']} → {item['after']} {item['unit']} | "
                         f"context: {'; '.join(item['periods']) or item['context']} | "
                         f"calculated Δ={item['delta']} {item['unit']}; {relative}"
                         + (f"; source reports {item['reported_percent']}%" if 'reported_percent' in item else '')))
    reserves = []
    for item in evidence['reserves']:
        if item['page'] in pages:
            reserves.append(item)
    def reserve_priority(item):
        if 'causality_reserve' in item['categories']:
            return 0
        if re.search(r"\bH\d+\b", item['text']):
            return 1
        if re.search(r"ne mentionne|not mentioned|ne sont pas v[ée]rifi|not verified", item['text'], re.I):
            return 2
        if 'fiction' in item['categories']:
            return 3
        if re.search(r"annon[çc]ait|pr[ée]vision|forecast", item['text'], re.I):
            return 4
        return 5
    reserves.sort(key=lambda item: (reserve_priority(item), len(item['text'])))
    if not rows and not reserves:
        return ''
    buckets = {}
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
        if fiction_selected and item['categories'] == ['fiction']:
            continue
        row = f"[Page {item['page']}] ({','.join(item['categories'])}) " + re.sub(r"\s+", ' ', item['text'])
        # Long excerpts remain intact on disk. Never silently truncate a quote.
        if used + len(row) + 1 <= max(0, limit - 240):
            selected.append(row)
            used += len(row) + 1
            fiction_selected |= 'fiction' in item['categories']
    # Use spare room for numeric rows that did not fit their initial share.
    for row in remaining_rows:
        if used + len(row) + 1 <= max(0, limit - 240):
            selected.append(row)
            used += len(row) + 1
    return ("\n\nSource evidence (untrusted data; lexical candidates and arithmetic, not scientific validation):\n"
            + '\n'.join(selected) + f"\nEvidence extracts omitted from this prompt: {len(rows) + len(reserves) - len(selected)}. "
            "Full provenance retained locally.\n")


def combined(notes, evidence, evidence_limit=EVIDENCE_PROMPT_CHARS):
    text = '\n\n'.join(f"## Notes {i + 1} | PDF pages: {','.join(map(str, item['pages']))}\n{item['text']}"
                       + ("\nEarlier-stage warning signals (may be resolved): " + ', '.join(item['warning_codes'])
                          if item.get('warning_codes') else '')
                       for i, item in enumerate(notes))
    pages = {p for item in notes for p in item['pages']}
    if evidence_limit == 0:
        return text + '\nSource evidence omitted from this prompt (context budget); full provenance retained locally.'
    return text + evidence_block(evidence, pages, evidence_limit)


def prepare_input(notes, evidence, fits):
    """Use exact existing context checks; fall back before adding merge calls.

    The compact retry is tokenized only when full evidence does not fit. Scope,
    warnings and raw notes remain; the evidence ledger itself is never trimmed.
    """
    text = combined(notes, evidence)
    if fits(text):
        return text, True, EVIDENCE_PROMPT_CHARS
    if evidence_block(evidence, {p for item in notes for p in item['pages']}):
        compact = combined(notes, evidence, 0)
        if fits(compact):
            return compact, True, 0
    return text, False, EVIDENCE_PROMPT_CHARS


def _normalized(value):
    return ' '.join(re.findall(r"\w+", unicodedata.normalize('NFKC', value).casefold()))


def _entity_in_line(entity, line):
    # Exact label, or an explicitly extracted leading code: A · School / A — School.
    normalized = _normalized(entity)
    candidate = _normalized(line)
    if normalized and re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", candidate):
        return True
    code = re.match(r"^([A-Za-z0-9]{1,8})\s*[·—–-]\s*\S", entity)
    return bool(code and re.search(r"(?:^\s*[-*]?\s*" + re.escape(code[1]) + r"\s*:|\(" + re.escape(code[1]) + r"\))", line))


def numeric_checks(answer, evidence, input_pages):
    """Flag only uniquely scoped, explicitly signed/labelled delta claims.

    Multiple records for an entity require exact periods/context in the claim,
    otherwise abstain. A possible mismatch is never an automatic correction.
    """
    issues, checked, skipped = [], 0, 0
    for line in answer.splitlines():
        quantities = list(QUANTITY.finditer(line))
        if not quantities:
            continue
        candidates = [item for item in evidence['comparisons'] if item['page'] in input_pages
                      and _entity_in_line(item['entity'], line)]
        refs = [int(m[1]) for m in re.finditer(r"\[Page (\d+)\]", line)]
        if refs:
            candidates = [item for item in candidates if item['page'] in refs]
        # Duplicate source rows from repeat headers must not resolve ambiguity.
        if len(candidates) != 1:
            skipped += 1
            continue
        item = candidates[0]
        claim_years = set(re.findall(r"\b(?:19|20|21)\d{2}\b", CITATION.sub('', line)))
        source_years = set(re.findall(r"\b(?:19|20|21)\d{2}\b", item['context']))
        if claim_years and not claim_years <= source_years:
            skipped += 1
            continue
        if item['unit'] == '%':
            # Percent change versus percentage points cannot be inferred here.
            skipped += 1
            continue
        # Only one absolute quantity (+ optional relative percentage) can be a
        # simple delta claim. Rows containing before/after values are not deltas.
        absolute = [q for q in quantities if q['unit'] != '%']
        if len(absolute) > 1:
            skipped += 1
            continue
        labelled_change = bool(re.search(r"\b(?:delta|variation|change|évolution)\b|Δ", line, re.I))
        relative_summary = any(q['unit'] == '%' and q['value'].startswith(('+', '-', '−', '–'))
                               for q in quantities)
        for quantity in quantities:
            signed = quantity['value'].startswith(('+', '-', '−', '–'))
            if not labelled_change and (not signed or (quantity['unit'] != '%' and not relative_summary)):
                skipped += 1
                continue
            unit = quantity['unit']
            if unit not in (item['unit'], '%'):
                skipped += 1
                continue
            expected = item['relative_percent'] if unit == '%' else item['delta']
            if expected is None:
                skipped += 1
                continue
            actual = decimal_value(quantity['value'])
            # Half a displayed decimal place: accept legitimate rounding only.
            tolerance = Decimal(1).scaleb(actual.as_tuple().exponent) / 2
            checked += 1
            if abs(actual - Decimal(expected)) > tolerance:
                issues.append({"code": "numeric_mismatch", "evidence_id": item['id'],
                               "entity": item['entity'], "page": item['page'], "unit": unit,
                               "expected": expected, "actual": str(actual)})
    return {"checked": checked, "unchecked_lines_or_values": skipped, "issues": issues,
            "status": "partial" if checked else "not_performed",
            "scope": "unique_entity_explicit_delta_only"}


def certainty_checks(answer, evidence, input_pages):
    """Lexical escalation signals only, never a causal/semantic verdict.

    Reserves stay available independently of generated notes. A warning does
    not prove two sentences concern the same intervention or protocol.
    """
    reserves = [item for item in evidence['reserves'] if item['page'] in input_pages]
    causal = [item for item in reserves if 'causality_reserve' in item['categories']]
    missing = [item for item in reserves if 'missing_information' in item['categories']
               and re.search(r"étalonnage|calibrat", item['text'], re.I)]
    issues = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", answer):
        if causal and re.search(r"a (?:entraîné|causé|permis)|\bcaused\b|\bled to\b", sentence, re.I) and not re.search(
                r"\b(?:pas|non|sans|pourrait|peut|not|no|may|might|could|without)\b", sentence, re.I):
            issues.append({'code': 'certainty_escalation_candidate', 'evidence_ids': [item['id'] for item in causal],
                           'detection': 'lexical_only'})
        if missing and re.search(r"aucun étalonnage.{0,35}(?:réalisé|effectué)|no calibration.{0,25}performed|"
                                 r"étalonnage n.a (?:pas|jamais) été (?:réalisé|effectué)", sentence, re.I):
            issues.append({'code': 'missing_information_escalation_candidate',
                           'evidence_ids': [item['id'] for item in missing], 'detection': 'lexical_only'})
    return issues


def quote_checks(answer, evidence, input_pages):
    """Locate exact reserve/row quotations only; this does not validate a claim."""
    items = [(item['page'], item['text']) for item in evidence['reserves']]
    items += [(item['page'], item['raw']) for item in evidence['comparisons']]
    issues = []
    for line in answer.splitlines():
        refs = {int(m[1]) for m in re.finditer(r"\[Page (\d+)\]", line)}
        if not refs:
            continue
        for match in re.finditer(r'«([^»]+)»|"([^"\n]+)"|“([^”]+)”', line):
            quote = _normalized(next(part for part in match.groups() if part is not None))
            if len(quote) < 30:
                continue
            source_pages = {page for page, text in items if quote in _normalized(text)}
            # A line can cite several claims; abstain if it includes the quote's source.
            if len(source_pages) == 1 and source_pages <= set(input_pages) and refs.isdisjoint(source_pages):
                issues.append({'code': 'quote_page_mismatch', 'reference': ', '.join(f'[Page {p}]' for p in sorted(refs)),
                               'matching_pages': sorted(source_pages), 'detection': 'exact_text_only'})
    return issues


def audit(answer, evidence, input_pages):
    citations = citation_checks(answer, evidence['pdf_pages'], input_pages,
                               set(evidence['text_pages']) - set(evidence['missing']))
    numeric = numeric_checks(answer, evidence, set(input_pages))
    return {"version": VERSION, "citations": citations, "numeric": numeric,
            "issues": citations['issues'] + numeric['issues']
                      + certainty_checks(answer, evidence, input_pages) + quote_checks(answer, evidence, input_pages),
            "semantic_validation": False}


MESSAGES = {
    'fr': {
        'title': 'Contrôles documentaires automatiques',
        'nonexistent_page': 'Référence à une page PDF inexistante',
        'page_outside_input': 'Page citée absente des entrées de cette étape',
        'page_without_text': 'Page citée sans texte extrait exploitable',
        'malformed_page_reference': 'Référence de page non interprétable',
        'numeric_mismatch': 'Écart numérique possible avec une ligne source',
        'certainty_escalation_candidate': 'Causalité potentiellement trop affirmative (signal lexical)',
        'missing_information_escalation_candidate': 'Absence de mention potentiellement transformée en absence de réalisation (signal lexical)',
        'quote_page_mismatch': 'Citation textuelle retrouvée sur une autre page source',
        'notice': "Avertissements, sans correction automatique. L’existence d’une page ne valide pas le sens de la citation. Les calculs hors des formats reconnus et la cohérence scientifique ne sont pas vérifiés.",
        'count': 'signalement(s) dans les étapes intermédiaires',
    },
    'en': {
        'title': 'Automatic document checks',
        'nonexistent_page': 'Reference to a nonexistent PDF page',
        'page_outside_input': 'Cited page absent from this step’s inputs',
        'page_without_text': 'Cited page has no usable extracted text',
        'malformed_page_reference': 'Unparseable page reference',
        'numeric_mismatch': 'Possible numerical mismatch with a source row',
        'certainty_escalation_candidate': 'Potentially overstated causality (lexical signal)',
        'missing_information_escalation_candidate': 'Potential change from not mentioned to not performed (lexical signal)',
        'quote_page_mismatch': 'Exact quotation found on another source page',
        'notice': 'Warnings, without automatic correction. Page existence does not validate citation meaning. Calculations outside recognized formats and scientific consistency are unchecked.',
        'count': 'warning(s) in intermediate steps',
    },
}


def report_warnings(checks, language):
    """Publish localized warnings, never silently remove invalid citations/results."""
    messages = MESSAGES['fr' if language.lower() in ('fr', 'french', 'français') else 'en']
    final = checks['steps'].get('rapport_final.md', {})
    issues = final.get('issues', [])
    earlier = sum(len(check.get('issues', [])) for name, check in checks['steps'].items() if name != 'rapport_final.md')
    if not issues and not earlier:
        return ''
    lines = [f"\n\n### {messages['title']}\n", messages['notice'], '']
    seen = set()
    for issue in issues:
        detail = issue.get('reference', '')
        if issue['code'] == 'numeric_mismatch':
            detail = f"{issue['entity']} (PDF {issue['page']}, {issue['unit']})"
        key = issue['code'], detail
        if key not in seen:
            lines.append(f"- {messages[issue['code']]}" + (f" : {detail}." if detail else '.'))
            seen.add(key)
    if earlier:
        lines.append(f"- {earlier} {messages['count']}.")
    return '\n'.join(lines) + '\n'
