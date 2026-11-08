"""Static contracts for existing dictionaries, not runtime validators or serializers.

Optional fields describe historical snapshots or incremental results. Constructing
and decoding JSON still follows the existing config/store/transport validation.
"""

from typing import NotRequired, Required, TypedDict


class ExtractionSettings(TypedDict):
    enabled: bool
    languages: str
    dpi: int
    max_pixels: int
    timeout: int
    max_pages: int
    version: NotRequired[int]


class ReportLanguage(TypedDict):
    report_language: str
    effective_report_language: NotRequired[str]


class TokenBudgets(TypedDict):
    chunk_tokens: int
    analysis_tokens: int
    merge_tokens: int
    final_tokens: int


class JobConfig(TokenBudgets, ReportLanguage):
    """Deployment defaults and resolved job controls, before private prompt fields.

    Keep language/profile/reasoning values as strings: legacy values and current
    validators determine compatibility, without new enum conversion or checks.
    """

    context_tokens: int
    request_timeout: int
    step_timeout_seconds: int
    max_calls: int
    max_attempts: int
    extraction: ExtractionSettings
    temperature: float | None
    top_p: float | None
    top_k: int | None
    min_p: float | None
    thinking_section: NotRequired[str]
    thinking_merge: NotRequired[str]
    thinking_final: NotRequired[str]
    thinking_adapter: NotRequired[str]
    initial_thinking: NotRequired[bool]
    apply_template: NotRequired[bool]
    usage_supported: NotRequired[bool]
    profile: NotRequired[str]
    profile_version: NotRequired[int]
    schema_version: NotRequired[int]
    pipeline_version: NotRequired[int]
    concurrency: NotRequired[int]
    detail_level: NotRequired[str]
    customized: NotRequired[bool]
    warnings: NotRequired[list[str]]


class DiagnosticConfig(JobConfig):
    """Client controls with an empty prompt, without the private snapshot templates."""

    system_prompt: str


class JobSnapshot(JobConfig, total=False):
    """Private saved configuration; upload adds source_hash after reading the PDF.

    A job with no snapshot is recomputed through the existing legacy path.
    Optional inherited fields retain the older snapshot fallback behavior.
    """

    system_prompt: Required[str]
    templates: Required[dict[str, str]]
    prompt_version: Required[int]
    source_hash: str


class PublicJobConfig(TypedDict, total=False):
    """Allowlisted controls; old records may omit any control, never fingerprint."""

    fingerprint: Required[str]
    profile: str
    profile_version: int
    customized: bool
    chunk_tokens: int
    context_tokens: int
    analysis_tokens: int
    merge_tokens: int
    final_tokens: int
    report_language: str
    detail_level: str
    temperature: float | None
    top_p: float | None
    top_k: int | None
    min_p: float | None
    concurrency: int
    thinking_section: str
    thinking_merge: str
    thinking_final: str
    warnings: list[str]
    max_calls: int
    step_timeout_seconds: int
    schema_version: int


class PageText(TypedDict):
    page: int
    text: str


class Chunk(TypedDict):
    text: str
    pages: list[int]
    # Historical chunks did not retain the lossless passage/page pairs.
    parts: NotRequired[list[PageText]]


class ExtractionResult(TypedDict):
    """The extracted.json object assembled by the pipeline."""

    pages: list[PageText]
    page_count: int
    missing: list[int]
    ocr_pages: list[int]


# extract_pdf() keeps its tuple API; JSON turns this tuple into a list as before.
PdfExtraction = tuple[list[PageText], int, list[int], list[int]]


class AnalysisNote(TypedDict):
    text: str
    pages: list[int]
    sections: list[int]
    warning_codes: NotRequired[list[str]]


class CompletionMetrics(TypedDict, total=False):
    """Known measurements: absent/None stays unknown, including partial previews."""

    prompt_tokens: float | None
    output_tokens: float | None
    reasoning_tokens: float | None
    useful_tokens: float | None
    cached_prompt_tokens: float | None
    evaluated_prompt_tokens: float | None
    prefill_ms: float | None
    decode_ms: float | None
    first_event_ms: float | None
    first_content_ms: float | None
    finish_reason: str | None
    prompt_tokens_source: str
    output_tokens_source: str
    reasoning_tokens_source: str
    useful_tokens_source: str
    cached_prompt_tokens_source: str
    evaluated_prompt_tokens_source: str
    prefill_ms_source: str
    decode_ms_source: str
    prefill_tokens_per_second: float
    decode_tokens_per_second: float
    output_budget: int
    request_started_at: float
    input_count: int
    input_count_source: str
    template_margin: int
    content_chars: int
    reasoning_chars: int
    request_ms: float
    error_code: str
    stop_category: str
    quality_checks: dict[str, object]


class ChatMessage(TypedDict):
    role: str
    content: str
