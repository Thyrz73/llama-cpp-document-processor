"""Report downloads and the bounded, translated Markdown viewer."""

import logging
import os
import stat
from contextlib import ExitStack, closing
from io import TextIOWrapper

from flask import Blueprint, abort, make_response, render_template, request, send_file
from werkzeug.exceptions import Conflict, NotFound

from .. import reports
from ..observability import event
from ..report_text import REPORT_FILE
from . import get_services, job_directory

blueprint = Blueprint("reports", __name__)


@blueprint.get("/api/jobs/<job_id>/report")
def report(job_id):
    """Stream the original Markdown with a descriptive attachment filename."""
    try:
        job, stream = resolve_report(job_id)
        with ExitStack() as cleanup:
            cleanup.callback(stream.close)
            name = ensure_report_name(job, stream)
            info = os.fstat(stream.fileno())
            response = send_file(
                stream,
                mimetype="text/markdown",
                as_attachment=True,
                download_name=name,
                conditional=False,
                last_modified=info.st_mtime,
                etag=f"{info.st_dev}-{info.st_ino}-{info.st_mtime_ns}-{info.st_size}",
            )
            cleanup.callback(response.close)
            response.content_length = info.st_size
            response.make_conditional(
                request.environ, accept_ranges=True, complete_length=info.st_size
            )
            event(
                "report_download",
                job_id=job_id,
                filename=name,
                bytes=response.content_length,
            )
            # Names are portable ASCII; retain the RFC 5987 form as well.
            response.headers["Content-Disposition"] += "; filename*=UTF-8''" + name
            response.headers["Content-Type"] = "text/markdown; charset=utf-8"
            # WSGI owns the stream through send_file's wrapper, including HEAD,
            # conditional responses and early client disconnects. Never reopen it.
            cleanup.pop_all()
            return response
    except OSError:
        event("report_missing", logging.WARNING, job_id=job_id, status="unavailable")
        abort(404)


def resolve_report(job_id):
    """Return (job, owned binary stream) for a finished job, or HTTP 404/409.

    The caller must close the stream or transfer it to the WSGI response.
    The server chooses the path and retains the store's job identity checks.
    <data> and its ancestors must be trusted and stable; jobs, the job directory
    and the report are opened without following links. Validate the opened file,
    not a pathname that could subsequently be substituted. Hard links and special
    files are rejected. Published report inodes must not be modified in place:
    streaming pins the file identity, not a snapshot of concurrently written bytes.
    """
    services = get_services()
    directory = job_directory(job_id)
    job = services.store.get_job(job_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        abort(409)
    with ExitStack() as directories:
        parent = None
        for component in (directory.parent, directory.name):
            parent = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            directories.callback(os.close, parent)
        descriptor = os.open(
            REPORT_FILE,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("Unsafe report file.")
            return job, os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise


def ensure_report_name(job, stream):
    """Return the report's download name.

    Jobs created before names were stored get one computed from the
    beginning of the opened report, which is then saved. Detach the text wrapper
    and rewind so naming never closes or consumes the response's binary stream.
    """
    services = get_services()
    if job.get("report_name"):
        return job["report_name"]
    text = TextIOWrapper(stream, encoding="utf-8", errors="replace")
    try:
        name = reports.report_filename(
            text.read(reports.TITLE_SCAN_CHARS), job["filename"]
        )
    finally:
        text.detach()
        stream.seek(0)
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
        job, stream = resolve_report(job_id)
        # Bounded read also handles a file growing between stat and read.
        with closing(stream):
            name = ensure_report_name(job, stream)
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
