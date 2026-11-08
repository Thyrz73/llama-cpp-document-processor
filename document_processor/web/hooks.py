"""Application-wide request protections, response headers and public errors."""

import logging
import time
from urllib.parse import urlsplit

from flask import g, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from .. import config as job_config
from ..observability import event
from . import get_services


def endpoint_name():
    return request.endpoint.rsplit(".", 1)[-1] if request.endpoint else None


def begin_request():
    """Measure request latency and reject browser writes originating on another site."""
    g.started = time.monotonic()
    origin = request.headers.get("Origin")
    if (
        request.method == "POST"
        and origin
        and (
            urlsplit(origin).netloc != request.host
            or urlsplit(origin).scheme not in ("http", "https")
        )
    ):
        return jsonify(
            error="Cross-origin writes are not allowed.",
            error_code="origin_rejected",
        ), 403


def finish_request(response):
    """Keep private job responses uncached and record download latency without content."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    if endpoint_name() in ("report", "view_report"):
        event(
            "report_response",
            job_id=(request.view_args or {}).get("job_id"),
            status=response.status_code,
            duration_ms=round((time.monotonic() - g.started) * 1000, 1),
            bytes=response.content_length,
        )
    if response.status_code >= 400 and request.method == "POST":
        event(
            "request_rejected",
            logging.WARNING,
            endpoint=endpoint_name(),
            status=response.status_code,
        )
    return response


def too_large(_):
    """Return JSON with HTTP 413 for an oversized request body.

    The _ parameter is Flask's exception and is unused here.
    """
    services = get_services()
    event("upload_rejected", logging.WARNING, error_code="upload_limit")
    return jsonify(
        error=f"PDF too large (limit: {services.settings.max_upload_mb} MiB)",
        error_code="upload_limit",
    ), 413


def invalid_configuration(exc):
    """Return stable validation codes for translated browser errors."""
    event("configuration_rejected", logging.WARNING, error_code=exc.code)
    return jsonify(error=str(exc), error_code=exc.code), 400


def api_error(exc):
    """Answer HTTP 400, 404, 409 and 500 with a generic JSON error, without exception details."""
    code = getattr(exc, "code", 500)
    messages = {
        400: "Invalid request.",
        404: "Job or file not found.",
        409: "The result is not ready.",
        500: "An internal error occurred.",
    }
    return jsonify(
        error=messages.get(code, "Request failed."), error_code=f"http_{code}"
    ), code


def register_hooks(app):
    app.before_request(begin_request)
    app.after_request(finish_request)
    app.register_error_handler(RequestEntityTooLarge, too_large)
    app.register_error_handler(job_config.ConfigurationError, invalid_configuration)
    for status in (400, 404, 409, 500):
        app.register_error_handler(status, api_error)
