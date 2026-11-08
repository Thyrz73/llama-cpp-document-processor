"""Turn the options of a new job into its validated configuration.

Options set application budgets and the sampling values sent with each
request; they never change the llama.cpp server's own settings.
"""

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, cast

from .contracts import JobConfig, PublicJobConfig
from .profiles import PROFILE_VERSION, PROFILES

INTEGER_LIMITS = {
    "chunk_tokens": (128, 131072),
    "analysis_tokens": (64, 65536),
    "merge_tokens": (64, 65536),
    "final_tokens": (64, 65536),
    "max_calls": (1, 2000),
    "step_timeout_seconds": (1, 86400),
}
RESUME_BUDGETS = {"analysis_tokens", "merge_tokens", "final_tokens"}


class ConfigurationError(ValueError):
    """Report a public validation code without echoing untrusted values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def fingerprint(value: object) -> str:
    """Return the SHA-256 of value encoded as canonical JSON, to detect changes.

    Keys are sorted, so equal values always give the same fingerprint. It is
    a checksum, not a proof of who wrote the value.
    """
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_options(raw: object) -> dict[str, Any]:
    """Decode the optional "options" form field: a JSON object of at most 8,192 characters."""
    if not raw:
        return {}
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ConfigurationError(
            "invalid_options", "Options must be a small JSON object."
        )
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ConfigurationError(
            "invalid_options", "Options must be valid JSON."
        ) from None
    if not isinstance(value, dict):
        raise ConfigurationError("invalid_options", "Options must be a JSON object.")
    return value


def resolve(
    defaults: JobConfig,
    options: object = None,
    capabilities: Mapping[str, object] | None = None,
) -> JobConfig:
    """Return a job configuration built from the deployment defaults and the job's options.

    A chosen profile replaces the default token budgets, the detail level
    scales the final budget, then validated overrides apply last. The context
    size and concurrency remain administrator settings. capabilities is the
    backend inspection, when known: an unknown server context adds a warning
    instead of a guessed value. Raise ConfigurationError for any invalid
    option or budget.
    """
    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - {
        "profile",
        "overrides",
        "report_language",
        "detail_level",
    }:
        raise ConfigurationError("invalid_options", "Unsupported job options.")
    # Options remain dynamic inside the existing validator; no values are converted.
    config = cast(dict[str, Any], copy.deepcopy(defaults))
    profile = options.get("profile", "legacy")
    if not isinstance(profile, str) or profile not in (*PROFILES, "legacy"):
        raise ConfigurationError("invalid_profile", "Unknown performance profile.")
    config.update(
        profile=profile,
        profile_version=PROFILE_VERSION,
        schema_version=1,
        pipeline_version=2,
        concurrency=1,
    )
    if profile != "legacy":
        config.update(PROFILES[profile])
    detail = options.get("detail_level", "standard")
    if detail not in ("concise", "standard", "detailed"):
        raise ConfigurationError("invalid_detail", "Unknown report detail level.")
    config["detail_level"] = detail
    # Detail is independent of the hardware profile. Explicit final_tokens wins.
    config["final_tokens"] = round(
        config["final_tokens"]
        * {"concise": 0.8, "standard": 1.0, "detailed": 1.5}[detail]
    )
    language = options.get("report_language", config["report_language"])
    if "report_language" in options and language not in ("auto", "fr", "en"):
        raise ConfigurationError("invalid_language", "Choose auto, fr or en.")
    config["report_language"] = language
    overrides = options.get("overrides", {})
    allowed = set(INTEGER_LIMITS) | {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "thinking_section",
        "thinking_merge",
        "thinking_final",
    }
    if not isinstance(overrides, dict) or set(overrides) - allowed:
        raise ConfigurationError(
            "invalid_overrides", "Unsupported advanced parameters."
        )
    config.update(overrides)
    config["customized"] = bool(overrides)
    _validate_integer_budgets(config)
    for name, maximum in (("temperature", 2), ("top_p", 1), ("min_p", 1)):
        value = config.get(name)
        if value is not None and (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= maximum
        ):
            raise ConfigurationError("invalid_sampling", f"Invalid {name} value.")
    if config.get("top_k") is not None and (
        type(config["top_k"]) is not int or not 0 <= config["top_k"] <= 1000
    ):
        raise ConfigurationError("invalid_sampling", "Invalid top_k value.")
    warnings = [] if profile == "legacy" else ["profile_uncalibrated"]
    extraction = config["extraction"]
    for name, maximum in (
        ("dpi", 600),
        ("max_pixels", 20000000),
        ("timeout", 3600),
        ("max_pages", 10000),
    ):
        if type(extraction[name]) is not int or not 1 <= extraction[name] <= maximum:
            raise ConfigurationError("invalid_extraction", "Invalid extraction limits.")
    if not re.fullmatch(r"[A-Za-z0-9_+.-]{1,100}", extraction["languages"]):
        raise ConfigurationError("invalid_extraction", "Invalid OCR languages.")
    if type(config["request_timeout"]) is not int or config["request_timeout"] < 1:
        raise ConfigurationError("invalid_budget", "Invalid network timeout.")
    if (
        type(config["max_attempts"]) is not int
        or not 1 <= config["max_attempts"] <= 100
    ):
        raise ConfigurationError("invalid_budget", "Invalid retry limit.")
    for stage in ("section", "merge", "final"):
        name = f"thinking_{stage}"
        if config[name] not in ("inherit", "on", "off"):
            raise ConfigurationError("invalid_thinking", "Unknown reasoning policy.")
        if config[name] != "inherit" and config["thinking_adapter"] == "unsupported":
            raise ConfigurationError(
                "thinking_unsupported",
                "Reasoning control has not been verified for this backend.",
            )
    warnings.extend(_validate_context(cast(JobConfig, config), capabilities))
    config["warnings"] = warnings
    return cast(JobConfig, config)


def increase_budgets(
    saved: JobConfig,
    increases: object,
    capabilities: Mapping[str, object] | None = None,
) -> tuple[JobConfig, list[str]]:
    """Return increased saved budgets and separate current context warnings.

    Values are absolute token counts, not deltas. Empty or unchanged budgets
    retain the snapshot exactly, including its historical metadata and warnings.
    Never resolve profiles, detail multipliers or deployment defaults again.
    The returned warnings describe this validation's backend observation only;
    they must not enter the snapshot, its fingerprint or artifact dependencies.
    """
    if not isinstance(increases, dict) or set(increases) - RESUME_BUDGETS:
        raise ConfigurationError(
            "invalid_overrides", "Only output token budgets can change on resume."
        )
    config = cast(dict[str, Any], copy.deepcopy(saved))
    config.update(increases)
    _validate_integer_budgets(config)
    for name, value in increases.items():
        if value < cast(Mapping[str, Any], saved)[name]:
            raise ConfigurationError(
                "invalid_budget", "Resume token budgets cannot decrease."
            )
    validation_warnings = _validate_context(cast(JobConfig, config), capabilities)
    return cast(JobConfig, config), validation_warnings


def _validate_integer_budgets(config: Mapping[str, object]) -> None:
    """Share strict integer and range checks between creation and resume."""
    for name, (minimum, maximum) in INTEGER_LIMITS.items():
        value = config[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ConfigurationError("invalid_budget", f"Invalid {name} budget.")


def _validate_context(
    config: JobConfig, capabilities: Mapping[str, object] | None
) -> list[str]:
    """Check joint budgets against the saved application and observed engine context."""
    context = config["context_tokens"]
    if type(context) is not int or context < 512:
        raise ConfigurationError(
            "invalid_context", "Invalid application context limit."
        )
    warnings = []
    engine_context = (capabilities or {}).get("context_tokens")
    if type(engine_context) is int and engine_context > 0:
        context = min(context, engine_context)
    else:
        warnings.append("engine_context_unknown")
    # This is a preliminary check. Each fully assembled prompt is checked later.
    if (
        config["chunk_tokens"] + config["analysis_tokens"] + 768 > context
        or max(config["merge_tokens"], config["final_tokens"]) + 768 >= context
    ):
        raise ConfigurationError(
            "context_insufficient",
            "Budgets exceed the available context; reduce them before starting.",
        )
    return warnings


def public_config(
    config: JobConfig, stored_fingerprint: str | None = None
) -> PublicJobConfig:
    """Expose only job controls, never prompt text, credentials or local paths.

    stored_fingerprint is the value saved with the snapshot; passing it avoids
    hashing the whole snapshot again. warnings retains the snapshot's historical
    observations; it does not describe the backend's current availability.
    """
    fields = {
        "profile",
        "profile_version",
        "customized",
        "chunk_tokens",
        "context_tokens",
        "analysis_tokens",
        "merge_tokens",
        "final_tokens",
        "report_language",
        "detail_level",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "concurrency",
        "thinking_section",
        "thinking_merge",
        "thinking_final",
        "warnings",
        "max_calls",
        "step_timeout_seconds",
        "schema_version",
    }
    # A read-only view permits the fixed allowlist without dynamic TypedDict indexing.
    values: Mapping[str, object] = config
    result = {key: values[key] for key in fields if key in config}
    result["fingerprint"] = stored_fingerprint or fingerprint(config)
    return cast(PublicJobConfig, result)
