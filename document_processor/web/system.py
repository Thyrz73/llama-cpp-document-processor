"""Page, health, profiles, configuration preview and read-only diagnostics."""

import re
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from .. import config as job_config
from .. import profiles
from . import get_services

blueprint = Blueprint("system", __name__)


@blueprint.get("/")
def index():
    """Serve the interface page with its upload limit and translations."""
    services = get_services()
    return render_template(
        "index.html",
        limit=services.settings.max_upload_mb,
        translations=services.translations,
    )


@blueprint.get("/api/health")
def health():
    """Return HTTP 200 without checking the model or worker."""
    return jsonify(status="ok")


@blueprint.get("/api/profiles")
def profile_info():
    """Return the profiles, the default job configuration and the cached backend capabilities."""
    services = get_services()
    capabilities = services.capabilities.get()
    defaults = job_config.resolve(services.runtime_defaults())
    context = (capabilities or {}).get("context_tokens")
    if type(context) is int and context > 0:
        defaults["warnings"].remove("engine_context_unknown")
    return jsonify(
        profiles=profiles.catalog(),
        defaults=job_config.public_config(defaults),
        capabilities=capabilities,
        thinking_supported=services.runtime_defaults()["thinking_adapter"]
        != "unsupported",
    )


@blueprint.post("/api/config/preview")
def config_preview():
    """Resolve job options sent as JSON for the settings preview; job creation checks them again."""
    services = get_services()
    if request.content_length and request.content_length > 8192:
        raise job_config.ConfigurationError("invalid_options", "Options are too large.")
    options = request.get_json()
    if not isinstance(options, dict):
        raise job_config.ConfigurationError(
            "invalid_options", "Options must be a JSON object."
        )
    capabilities = services.capabilities.get()
    config = job_config.resolve(services.runtime_defaults(), options, capabilities)
    return jsonify(
        configuration=job_config.public_config(config),
        capabilities=capabilities,
        validation="preliminary",
    )


@blueprint.get("/api/diagnostics")
def diagnostics():
    """Return the backend inspection, cached or made now, and this process's memory use.

    The inspection is sanitized and never asks the model to generate anything.
    """
    services = get_services()
    value = services.inspect_diagnostics()
    process_rss_kib = None
    try:
        matched = re.search(
            r"^VmRSS:\s+(\d+)",
            Path("/proc/self/status").read_text(encoding="utf-8"),
            re.M,
        )
        if matched:
            process_rss_kib = int(matched[1])
    except OSError:
        pass
    return jsonify(
        backend=value,
        application={
            "context_limit": services.settings.context_tokens,
            "concurrency": 1,
            "process_rss_kib": process_rss_kib,
            "resource_scope": "processor_only",
        },
        engine_settings="read_only",
        engine_memory=None,
        engine_swap_rate=None,
    )
