"""Compatibility facade for the former reliability module.

Source extraction/selection lives in evidence, mechanical checks in checks,
and localized report text in report_text. Existing imports keep the same
functions, constants and version; new code can import their owners directly.
Private-helper instrumentation should patch the owning module.
"""

from .checks import CITATION as CITATION
from .checks import PAGE_REFERENCE as PAGE_REFERENCE
from .checks import PreparedEvidence as PreparedEvidence
from .checks import _citation_parts as _citation_parts
from .checks import _entity_in_line as _entity_in_line
from .checks import _normalized as _normalized
from .checks import audit as audit
from .checks import certainty_checks as certainty_checks
from .checks import citation_checks as citation_checks
from .checks import compare_quantities as compare_quantities
from .checks import numeric_checks as numeric_checks
from .checks import prepare_evidence as prepare_evidence
from .checks import quote_checks as quote_checks
from .evidence import _RESERVE_PATTERNS as _RESERVE_PATTERNS
from .evidence import ARROW as ARROW
from .evidence import EVIDENCE_PROMPT_CHARS as EVIDENCE_PROMPT_CHARS
from .evidence import NUMBER as NUMBER
from .evidence import QUANTITY as QUANTITY
from .evidence import RESERVES as RESERVES
from .evidence import UNIT as UNIT
from .evidence import VERSION as VERSION
from .evidence import _clean_entity as _clean_entity
from .evidence import _comparison as _comparison
from .evidence import build_evidence as build_evidence
from .evidence import combined as combined
from .evidence import decimal_value as decimal_value
from .evidence import derived as derived
from .evidence import evidence_block as evidence_block
from .evidence import extract_comparisons as extract_comparisons
from .evidence import merge_note as merge_note
from .evidence import note as note
from .evidence import prepare_input as prepare_input
from .report_text import MESSAGES as MESSAGES
from .report_text import report_warnings as report_warnings
