"""Bounded, deterministic report naming and restrictive Markdown rendering."""

import re
import unicodedata
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

PREVIEW_MAX_BYTES = 1024 * 1024
TITLE_SCAN_CHARS = 65536


def portable_stem(text):
    """Keep portable ASCII words without path separators or control characters."""
    text = (
        text.replace("œ", "oe")
        .replace("Œ", "Oe")
        .replace("æ", "ae")
        .replace("Æ", "Ae")
        .replace("ß", "ss")
    )
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_-")


def report_filename(markdown, original_filename):
    """Choose an existing descriptive heading, then fall back to the PDF name.

    Only the first 65,536 characters are inspected. Code fences are parsed by the
    Markdown library, so their apparent headings cannot become filenames.
    """
    parser = MarkdownIt("commonmark", {"html": False})
    tokens = parser.parse(markdown[:TITLE_SCAN_CHARS])
    generic = {
        "analyse",
        "analysis",
        "rapport",
        "report",
        "rapport final",
        "final report",
        "analyse du document",
        "document analysis",
        "analyse de document",
        "resume",
        "summary",
        "executive summary",
        "synthese",
        "synthese generale",
        "introduction",
        "conclusion",
        "conclusions",
        "resultats",
        "results",
        "methodes",
        "methods",
        "faits",
        "facts",
        "limites",
        "limitations",
        "objectifs",
        "objectives",
        "key findings",
        "recommandations",
        "recommendations",
        "document",
        "fichier",
        "file",
        "analyse du fichier",
        "analysis of the document",
    }
    original_stem = re.sub(
        r"(?i)\.pdf$", "", original_filename.replace("\\", "/").split("/")[-1]
    )
    chosen = ""
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open":
            continue
        inline = tokens[index + 1]
        title = "".join(
            child.content
            for child in (inline.children or [])
            if child.type in ("text", "code_inline")
        )
        # Application-generated metadata titles describe the file, not its subject.
        if re.search(r"(?i)\.pdf\b", title):
            continue
        title = re.sub(
            r"(?i)^\s*(?:analyse(?:\s+(?:du|de|des|de la|de l[’']))?"
            r"(?:\s+(?:document|fichier))?|analysis(?:\s+of(?:\s+the)?(?:\s+document)?)?"
            r"|rapport(?:\s+final)?|final\s+report|report)\s*[:–—-]\s*",
            "",
            title,
        )
        stem = portable_stem(title.replace("–", "-").replace("—", "-"))
        if stem.replace("_", " ").lower() in generic or not re.search(
            r"[A-Za-z]{3}", stem
        ):
            continue
        chosen = stem[:1].upper() + stem[1:].lower()
        break
    stem = chosen or portable_stem(original_stem) or "document"
    stem = re.sub(r"(?i)_rapport$", "", stem) or "document"
    return stem[:89].rstrip("_-") + "_rapport.md"


class _SafeLinkMarkdown(MarkdownIt):
    """Accept only relative, web and mail links in addition to the default checks."""

    def validateLink(self, url: str) -> bool:
        try:
            return super().validateLink(url) and urlsplit(url).scheme.lower() in (
                "",
                "http",
                "https",
                "mailto",
            )
        except ValueError:
            return False


def render_markdown(text):
    """Render text without HTML, images, executable links or remote resources."""
    parser = _SafeLinkMarkdown(
        "commonmark", {"html": False, "breaks": True, "maxNesting": 20}
    )
    parser.enable("table").disable("image")
    return parser.render(text)
