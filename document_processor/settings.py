"""Immutable deployment values, without environment reads or services at import time.

from_env() preserves the environment.py checks performed when app.py loads.
Job-level limits and cross-budget validation remain in config.resolve() at server
startup and job creation; reading deployment values never reads the prompt file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from . import environment
from .contracts import JobConfig


@dataclass(frozen=True, slots=True)
class Settings:
    """Process configuration containing only immutable scalars and paths.

    Credentials, backend URLs (which may embed credentials) and local paths are
    omitted from repr. job_defaults() creates fresh, credential-free dictionaries.
    """

    app_port: int
    data_dir: Path = field(repr=False)
    llama_base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    max_upload_mb: int
    max_pages: int
    chunk_tokens: int
    context_tokens: int
    output_language: str
    analysis_tokens: int
    merge_tokens: int
    final_tokens: int
    request_timeout: int
    step_timeout: int
    max_model_calls: int
    max_job_attempts: int
    live_output_enabled: bool
    live_preview_chars: int
    ocr_enabled: bool
    ocr_languages: str
    ocr_dpi: int
    ocr_max_pixels: int
    ocr_timeout: int
    thinking_adapter: str
    initial_thinking: bool
    apply_template_supported: bool
    usage_supported: bool
    system_prompt_file: Path = field(repr=False)

    @classmethod
    def from_env(cls) -> Self:
        """Read the process environment once; do not load .env or touch files.

        Keep the original parsing order, fallback rules and SettingsError messages.
        Paths and API keys retain their original whitespace; only the existing
        text/URL/number/flag readers strip it. Live length is clamped, not rejected.
        """
        app_port = environment.integer("APP_PORT", 9932, maximum=65535)
        data_dir = Path(os.getenv("DATA_DIR") or "/app/data")
        llama_base_url = environment.http_url(
            "LLAMA_BASE_URL", "http://host.docker.internal:8080"
        )
        api_key = os.getenv("LLAMA_API_KEY", "")
        max_upload_mb = environment.integer("MAX_UPLOAD_MB", 20)
        max_pages = environment.integer("MAX_PAGES", 120)
        chunk_tokens = environment.integer("CHUNK_TOKENS", 3000)
        context_tokens = environment.integer("CONTEXT_TOKENS", 16384)
        output_language = os.getenv("OUTPUT_LANGUAGE", "auto").strip() or "auto"
        analysis_tokens = environment.integer("ANALYSIS_TOKENS", 1000)
        merge_tokens = environment.integer("MERGE_TOKENS", 1200)
        final_tokens = environment.integer("FINAL_TOKENS", 1800)
        request_timeout = environment.integer("REQUEST_TIMEOUT_SECONDS", 21600)
        step_timeout = environment.integer("STEP_TIMEOUT_SECONDS", request_timeout)
        max_model_calls = environment.integer("MAX_MODEL_CALLS", 500)
        max_job_attempts = environment.integer("MAX_JOB_ATTEMPTS", 10)
        live_output_enabled = environment.flag("LIVE_OUTPUT_ENABLED", True)
        live_preview_chars = max(
            1000, min(environment.integer("LIVE_PREVIEW_CHARS", 12000), 50000)
        )
        ocr_enabled = environment.flag("OCR_ENABLED", True)
        ocr_languages = os.getenv("OCR_LANGUAGES", "").strip() or "eng+fra"
        ocr_dpi = environment.integer("OCR_DPI", 150)
        ocr_max_pixels = environment.integer("OCR_MAX_PIXELS", 5000000)
        ocr_timeout = environment.integer("OCR_TIMEOUT_SECONDS", 180)
        thinking_adapter = environment.choice(
            "LLAMA_THINKING_ADAPTER",
            "unsupported",
            ("unsupported", "chat_template", "prompt"),
        )
        initial_thinking = environment.flag("LLAMA_INITIAL_THINKING", False)
        apply_template_supported = environment.flag(
            "LLAMA_APPLY_TEMPLATE_SUPPORTED", False
        )
        usage_supported = environment.flag("LLAMA_USAGE_SUPPORTED", False)
        system_prompt_file = Path(
            os.getenv("SYSTEM_PROMPT_FILE") or "/app/config/system_prompt.txt"
        )
        return cls(
            app_port=app_port,
            data_dir=data_dir,
            llama_base_url=llama_base_url,
            api_key=api_key,
            max_upload_mb=max_upload_mb,
            max_pages=max_pages,
            chunk_tokens=chunk_tokens,
            context_tokens=context_tokens,
            output_language=output_language,
            analysis_tokens=analysis_tokens,
            merge_tokens=merge_tokens,
            final_tokens=final_tokens,
            request_timeout=request_timeout,
            step_timeout=step_timeout,
            max_model_calls=max_model_calls,
            max_job_attempts=max_job_attempts,
            live_output_enabled=live_output_enabled,
            live_preview_chars=live_preview_chars,
            ocr_enabled=ocr_enabled,
            ocr_languages=ocr_languages,
            ocr_dpi=ocr_dpi,
            ocr_max_pixels=ocr_max_pixels,
            ocr_timeout=ocr_timeout,
            thinking_adapter=thinking_adapter,
            initial_thinking=initial_thinking,
            apply_template_supported=apply_template_supported,
            usage_supported=usage_supported,
            system_prompt_file=system_prompt_file,
        )

    @property
    def max_upload_bytes(self) -> int:
        """The PDF byte limit, excluding the multipart envelope."""
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_content_length(self) -> int:
        """Flask's request limit, including the existing multipart allowance."""
        return self.max_upload_bytes + 65536

    def job_defaults(self) -> JobConfig:
        """Build fresh job defaults; config.resolve() validates their joint limits."""
        return {
            "chunk_tokens": self.chunk_tokens,
            "context_tokens": self.context_tokens,
            "analysis_tokens": self.analysis_tokens,
            "merge_tokens": self.merge_tokens,
            "final_tokens": self.final_tokens,
            "report_language": self.output_language,
            "temperature": 0.2,
            "top_p": None,
            "top_k": None,
            "min_p": None,
            "thinking_section": "inherit",
            "thinking_merge": "inherit",
            "thinking_final": "inherit",
            "thinking_adapter": self.thinking_adapter,
            "initial_thinking": self.initial_thinking,
            "apply_template": self.apply_template_supported,
            "usage_supported": self.usage_supported,
            "request_timeout": self.request_timeout,
            "step_timeout_seconds": self.step_timeout,
            "max_calls": self.max_model_calls,
            "max_attempts": self.max_job_attempts,
            "extraction": {
                "version": 3,
                "enabled": self.ocr_enabled,
                "languages": self.ocr_languages,
                "dpi": self.ocr_dpi,
                "max_pixels": self.ocr_max_pixels,
                "timeout": self.ocr_timeout,
                "max_pages": self.max_pages,
            },
        }
