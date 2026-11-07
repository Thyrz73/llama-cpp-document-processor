"""Deterministic report text and its historical physical filename.

These FR/EN messages surround model output; they are not model instructions.
Browser translations live in static/translations.json and are not loaded here.
Task templates and language instructions remain in prompts.py.
"""

from collections.abc import Sequence

from .contracts import ReportLanguage

# Keep this historical name for existing reports, manifests and dependency hashes.
REPORT_FILE = "rapport_final.md"

MESSAGES = {
    "fr": {
        "title": "Contrôles documentaires automatiques",
        "nonexistent_page": "Référence à une page PDF inexistante",
        "page_outside_input": "Page citée absente des entrées de cette étape",
        "page_without_text": "Page citée sans texte extrait exploitable",
        "malformed_page_reference": "Référence de page non interprétable",
        "numeric_mismatch": "Écart numérique possible avec une ligne source",
        "certainty_escalation_candidate": "Causalité potentiellement trop affirmative (signal lexical)",
        "missing_information_escalation_candidate": "Absence de mention potentiellement transformée en absence de réalisation (signal lexical)",
        "quote_page_mismatch": "Citation textuelle retrouvée sur une autre page source",
        "notice": "Avertissements, sans correction automatique. L’existence d’une page ne valide pas le sens de la citation. Les calculs hors des formats reconnus et la cohérence scientifique ne sont pas vérifiés.",
        "count": "signalement(s) dans les étapes intermédiaires",
    },
    "en": {
        "title": "Automatic document checks",
        "nonexistent_page": "Reference to a nonexistent PDF page",
        "page_outside_input": "Cited page absent from this step’s inputs",
        "page_without_text": "Cited page has no usable extracted text",
        "malformed_page_reference": "Unparseable page reference",
        "numeric_mismatch": "Possible numerical mismatch with a source row",
        "certainty_escalation_candidate": "Potentially overstated causality (lexical signal)",
        "missing_information_escalation_candidate": "Potential change from not mentioned to not performed (lexical signal)",
        "quote_page_mismatch": "Exact quotation found on another source page",
        "notice": "Warnings, without automatic correction. Page existence does not validate citation meaning. Calculations outside recognized formats and scientific consistency are unchecked.",
        "count": "warning(s) in intermediate steps",
    },
}


def is_french(language: str) -> bool:
    """Tell whether a report language setting means French ("fr", "french" or "français")."""
    return language.lower() in ("fr", "french", "français")


def deterministic_language(config: ReportLanguage) -> str:
    """Read the saved text language, retaining historical behavior for old snapshots."""
    return config.get("effective_report_language", config["report_language"])


def report_header(
    config: ReportLanguage,
    filename: str,
    pages: int,
    chunks: int,
    missing: Sequence[int],
    ocr_pages: Sequence[int],
) -> str:
    """Return the Markdown header of the report, in the report language.

    It gives the PDF name, the page and section counts, and the pages that
    needed OCR or still lack usable text.
    """
    french = is_french(deterministic_language(config))
    title, page_label, chunk_label = (
        ("Analyse de", "Pages PDF", "sections analysées")
        if french
        else ("Analysis of", "PDF pages", "analyzed chunks")
    )
    # A filename is untrusted text even though the browser renders it safely.
    filename = (
        filename.replace("\n", " ")
        .replace("\r", " ")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    header = f"# {title} {filename}\n\n*{page_label}: {pages} — {chunk_label}: {chunks}.*\n\n"
    if missing:
        label = (
            "Texte insuffisant après extraction/OCR aux pages"
            if french
            else "Insufficient text after extraction/OCR on pages"
        )
        header += f"> {label}: {', '.join(map(str, missing))}.\n\n"
    if ocr_pages:
        label = "OCR utilisé aux pages" if french else "OCR used on pages"
        header += f"*{label}: {', '.join(map(str, ocr_pages))}.*\n\n"
    return header


def report_warnings(checks, language):
    """Return the warnings section appended to the final report, or "" when there is none.

    It lists the final report's warnings in the report language and counts
    those of earlier steps. The pipeline appends it to the report: the
    model's text itself is left unchanged, nothing is removed or corrected.
    """
    messages = MESSAGES["fr" if is_french(language) else "en"]
    final = checks["steps"].get(REPORT_FILE, {})
    issues = final.get("issues", [])
    earlier = sum(
        len(check.get("issues", []))
        for name, check in checks["steps"].items()
        if name != REPORT_FILE
    )
    if not issues and not earlier:
        return ""
    lines = [f"\n\n### {messages['title']}\n", messages["notice"], ""]
    seen = set()
    for issue in issues:
        detail = issue.get("reference", "")
        if issue["code"] == "numeric_mismatch":
            detail = f"{issue['entity']} (PDF {issue['page']}, {issue['unit']})"
        key = issue["code"], detail
        if key not in seen:
            lines.append(
                f"- {messages[issue['code']]}" + (f" : {detail}." if detail else ".")
            )
            seen.add(key)
    if earlier:
        lines.append(f"- {earlier} {messages['count']}.")
    return "\n".join(lines) + "\n"
