"""Report downloads and the bounded, translated Markdown viewer."""

import logging

from flask import Blueprint, abort, make_response, render_template, request, send_file
from werkzeug.exceptions import Conflict, NotFound

from .. import reports
from ..jobs import REPORT_FILE
from ..observability import event
from . import get_services, job_directory

blueprint = Blueprint("reports", __name__)


@blueprint.get("/api/jobs/<job_id>/report")
def report(job_id):
    """Stream the original Markdown with a descriptive attachment filename."""
    try:
        job, path = resolve_report(job_id)
        name = ensure_report_name(job, path)
        response = send_file(
            path, mimetype="text/markdown", as_attachment=True, download_name=name
        )
    except OSError:
        event("report_missing", logging.WARNING, job_id=job_id, status="unavailable")
        abort(404)
    event(
        "report_download", job_id=job_id, filename=name, bytes=response.content_length
    )
    # Names are portable ASCII; include the RFC 5987 form for UTF-8-aware clients too.
    response.headers["Content-Disposition"] += "; filename*=UTF-8''" + name
    response.headers["Content-Type"] = "text/markdown; charset=utf-8"
    return response


def resolve_report(job_id):
    """Return (job, report path) for a finished job, or answer HTTP 404 or 409.

    The server chooses the path: a request can never name a file.
    """
    services = get_services()
    directory = job_directory(job_id)
    job = services.store.get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        abort(409)
    path = directory / REPORT_FILE
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve().parent != directory.resolve()
    ):
        event("report_missing", logging.WARNING, job_id=job_id, status="unavailable")
        abort(404)
    return job, path


def ensure_report_name(job, path):
    """Return the report's download name.

    Jobs created before names were stored get one computed from the
    beginning of the report, which is then saved.
    """
    services = get_services()
    if job.get("report_name"):
        return job["report_name"]
    with path.open(encoding="utf-8", errors="replace") as stream:
        name = reports.report_filename(
            stream.read(reports.TITLE_SCAN_CHARS), job["filename"]
        )
    services.store.set_report_name(job["id"], name)
    return name


@blueprint.get("/api/jobs/<job_id>/report/view")
def view_report(job_id):
    """Render the report as HTML in the report viewer page.

    A report larger than reports.PREVIEW_MAX_BYTES is not rendered; it can
    still be downloaded.
    """
    services = get_services()
    language = request.args.get("lang", "en")
    if language not in ("en", "fr"):
        language = "en"

    def translate(key):
        return services.translations[language].get(key, key)

    try:
        job, path = resolve_report(job_id)
        name = ensure_report_name(job, path)
        # Bounded read also handles a file growing between stat and read.
        with path.open("rb") as stream:
            content = stream.read(reports.PREVIEW_MAX_BYTES + 1)
        if len(content) > reports.PREVIEW_MAX_BYTES:
            html, message, status = (
                "",
                "Preview is limited to 1 MiB. Download the original Markdown.",
                413,
            )
        else:
            html = (
                reports.render_markdown(content.decode("utf-8"))
                if content.strip()
                else ""
            )
            message, status = ("" if html else "No content is available."), 200
    except (OSError, UnicodeError):
        event("report_unreadable", logging.WARNING, job_id=job_id)
        name, html, message, status = (
            translate("Markdown report"),
            "",
            "Report not found or unreadable.",
            404,
        )
    except (NotFound, Conflict) as exc:
        name, html, message, status = (
            translate("Markdown report"),
            "",
            "Report not found or not ready.",
            exc.code,
        )
    event("report_view", job_id=job_id, status=status)
    response = make_response(
        render_template(
            "report.html",
            report_name=name,
            content=html,
            message=message,
            job_id=job_id,
            available=status in (200, 413),
            language=language,
            t=translate,
        ),
        status,
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'none'; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
