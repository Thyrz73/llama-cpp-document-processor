"""Jobs API: upload, status, history deletion, live previews, metrics and actions."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from flask import Blueprint, abort, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from .. import config as job_config
from .. import prompts
from ..files import file_hash
from ..jobs import PREVIEW_FILE, SOURCE_FILE, is_job_id
from ..observability import event, failure
from . import get_services, job_directory

blueprint = Blueprint("jobs", __name__)


@blueprint.get("/api/jobs")
def list_jobs():
    """Return the 100 most recent jobs and activity across the whole queue.

    has_active_jobs includes older jobs outside the visible list so the
    browser keeps polling while any job is queued or running.
    """
    services = get_services()
    jobs, has_active_jobs = services.store.list_jobs(100)
    return jsonify(
        jobs=[services.public_job(job) for job in jobs], has_active_jobs=has_active_jobs
    )


@blueprint.get("/api/jobs/<job_id>")
def job_info(job_id):
    """Return job_id and its verified public configuration, or HTTP 404."""
    services = get_services()
    job = services.store.get_job(job_id)
    if not job:
        abort(404)
    config = services.store.load_config(job_id)
    if config:
        job["configuration"] = job_config.public_config(config)
    return jsonify(services.public_job(job))


def delete_history_job(job_id):
    """Delete one finished job and return (result dictionary, HTTP status).

    The database record is deleted first and the files afterwards, so a file
    cleanup problem only adds a warning_code to a successful result.
    """
    services = get_services()
    if not is_job_id(job_id):
        return {"id": job_id, "deleted": False, "error_code": "invalid_job_id"}, 400
    try:
        error_code = services.store.delete_job(job_id)
    except sqlite3.Error as exc:
        failure("history_delete_failed", exc, job_id=job_id)
        return {"id": job_id, "deleted": False, "error_code": "delete_database"}, 500
    if error_code:
        return {
            "id": job_id,
            "deleted": False,
            "error_code": error_code,
        }, 404 if error_code == "job_not_found" else 409
    warning = services.store.remove_files(job_id)
    event(
        "history_deleted",
        job_id=job_id,
        status="files_retained" if warning else "deleted",
    )
    return {"id": job_id, "deleted": True, "warning_code": warning}, 200


@blueprint.post("/api/jobs/<job_id>/delete")
def delete_job(job_id):
    """Delete one terminal analysis using only its server-generated ID."""
    result, status = delete_history_job(job_id)
    return jsonify(result), status


@blueprint.post("/api/jobs/delete")
def delete_jobs():
    """Delete up to 100 finished jobs by ID; return one result per unique ID, with HTTP 207 if any fails."""
    if request.content_length and request.content_length > 8192:
        return jsonify(error_code="invalid_job_ids"), 400
    payload = request.get_json(silent=True)
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 100
        or not all(is_job_id(value) for value in ids)
    ):
        return jsonify(error_code="invalid_job_ids"), 400
    results = [delete_history_job(job_id)[0] for job_id in dict.fromkeys(ids)]
    return jsonify(results=results), 200 if all(
        item["deleted"] for item in results
    ) else 207


@blueprint.post("/api/jobs")
def create_job():
    """Save an uploaded PDF and queue a job for it.

    Read the multipart fields "file", "question" (the default instructions
    when empty) and "options". Resolve the options first, then check the
    question length, the .pdf extension and the PDF header; the worker checks
    the page count. Return HTTP 201 with the ID, status and public
    configuration, 400 for an invalid request or 413 for an oversized file.
    The saved PDF is removed if the job cannot be created.
    """
    services = get_services()
    options = job_config.parse_options(request.form.get("options"))
    config = services.new_snapshot(options, services.capabilities.get())
    uploaded = request.files.get("file")
    question = request.form.get("question") or services.fallback_question
    if not uploaded or not uploaded.filename:
        return jsonify(error="Select a PDF file", error_code="invalid_pdf"), 400
    if not question.strip() or len(question) > services.max_question_chars:
        return jsonify(
            error=f"Instructions required (maximum {services.max_question_chars} characters)",
            error_code="invalid_question",
        ), 400
    config["effective_report_language"] = prompts.resolve_report_language(
        config["report_language"], question
    )
    filename = uploaded.filename.replace("\\", "/").split("/")[-1][:120]
    if not filename.lower().endswith(".pdf"):
        return jsonify(
            error="Only .pdf files are accepted", error_code="invalid_pdf"
        ), 400
    if uploaded.stream.read(5) != b"%PDF-":
        return jsonify(
            error="The file does not appear to be a valid PDF", error_code="invalid_pdf"
        ), 400
    uploaded.stream.seek(0)
    job_id = uuid.uuid4().hex
    directory = job_directory(job_id)
    directory.mkdir(parents=True, mode=0o700)
    source = directory / SOURCE_FILE
    try:
        uploaded.save(source)
        if source.stat().st_size > services.settings.max_upload_bytes:
            raise RequestEntityTooLarge()
        config["source_hash"] = file_hash(source)
        services.store.create_job(
            job_id,
            filename,
            question,
            int(not services.is_default_question(question)),
            config,
        )
    except Exception:
        source.unlink(missing_ok=True)
        directory.rmdir()
        raise
    services.worker.wake()
    event(
        "job_created",
        job_id=job_id,
        filename=filename,
        bytes=source.stat().st_size,
        profile=config["profile"],
        custom_prompt=not services.is_default_question(question),
        chunk_tokens=config["chunk_tokens"],
        analysis_tokens=config["analysis_tokens"],
        merge_tokens=config["merge_tokens"],
        final_tokens=config["final_tokens"],
        report_language=config["report_language"],
    )
    return jsonify(
        id=job_id, status="queued", configuration=job_config.public_config(config)
    ), 201


@blueprint.get("/api/jobs/<job_id>/live")
def job_live(job_id):
    """Return the latest live preview of job_id, or HTTP 404.

    Prefer the process-local cache, falling back to JSON checkpoints without
    caching disk reads. A previous attempt cannot appear as the current pass.
    Only a running job is active; reasoning requires include_reasoning=true.
    """
    services = get_services()
    job, attempt_id = services.store.get_live_job(job_id)
    if not job:
        abort(404)
    path = job_directory(job_id) / PREVIEW_FILE
    settings = services.settings
    if not settings.live_output_enabled:
        return jsonify(
            phase=job["stage"],
            content="",
            reasoning="",
            active=False,
            enabled=settings.live_output_enabled,
        )
    payload = services.store.previews.get(job_id)
    if payload is not None and payload.get("_attempt_id") != attempt_id:
        services.store.previews.discard(job_id)
        payload = None
    if payload is None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            payload = None
    # Queued jobs have no current output, including legacy retries without job_runs.
    if (
        not isinstance(payload, dict)
        or job["status"] == "queued"
        or (job["status"] == "running" and job["stage"] == "Starting")
        or (
            payload.get("_attempt_id") is not None
            and payload["_attempt_id"] != attempt_id
        )
        or (
            attempt_id is not None
            and job["status"] == "running"
            and payload.get("_attempt_id") != attempt_id
        )
    ):
        return jsonify(
            phase=job["stage"],
            phase_code=job["phase_code"],
            phase_args=job["phase_args"],
            content="",
            reasoning="",
            active=False,
            enabled=True,
        )
    payload.pop("_attempt_id", None)
    for field in ("content", "reasoning"):
        value = payload.get(field, "")
        payload[field] = (
            value[-settings.live_preview_chars :] if isinstance(value, str) else ""
        )
    # The last file may have been written before the job completed or failed.
    payload["active"] = bool(payload.get("active") and job["status"] == "running")
    if payload["active"] and payload.get("started_at"):
        # Keep ordinary exception handling; suppress also handles ExceptionGroup.
        try:  # noqa: SIM105
            payload["elapsed_seconds"] = round(
                max(
                    0,
                    (
                        datetime.now(UTC)
                        - datetime.fromisoformat(payload["started_at"])
                    ).total_seconds(),
                ),
                1,
            )
        except (ValueError, TypeError):
            pass
    payload["enabled"] = True
    if request.args.get("include_reasoning") != "true":
        payload["reasoning"] = ""
    payload["reasoning_available"] = bool(payload.get("reasoning_chars"))
    payload["phase_code"] = job["phase_code"]
    payload["phase_args"] = job["phase_args"]
    return jsonify(payload)


@blueprint.get("/api/jobs/<job_id>/metrics")
def job_metrics(job_id):
    """Return a job's attempts and step measurements, paged with limit (1-500) and before.

    Measurements never contain document text, prompts or model output.
    """
    services = get_services()
    if not services.store.get_job(job_id):
        abort(404)
    try:
        limit = int(request.args.get("limit", "200"))
        before = int(request.args["before"]) if "before" in request.args else None
        if not 1 <= limit <= 500 or (before is not None and not 0 < before < 2**63):
            raise ValueError()
    except ValueError:
        abort(400)
    return jsonify(services.store.metrics(job_id, limit, before))


@blueprint.post("/api/jobs/<job_id>/retry")
def retry_job(job_id):
    """Queue a failed or cancelled job again, with its original configuration.

    Finished steps are reused. Return HTTP 200, 400 when the request has a
    body (a retry cannot change the settings), 404 for an unknown job, or 409
    when the job is neither failed nor cancelled or has used all its attempts.
    """
    services = get_services()
    if not services.store.get_job(job_id):
        abort(404)
    if request.data or request.form:
        return jsonify(
            error="Retry uses the original snapshot. Create a new job to change options.",
            error_code="immutable_config",
        ), 400
    outcome, attempts = services.store.requeue(
        job_id, services.settings.max_job_attempts
    )
    if outcome == "limit":
        return jsonify(
            error="The retry limit was reached. Create a new analysis.",
            error_code="retry_limit",
        ), 409
    if outcome == "conflict":
        return jsonify(
            error="Only failed or cancelled jobs can be retried",
            error_code="retry_conflict",
        ), 409
    services.worker.wake()
    legacy = services.store.load_config(job_id) is None
    event(
        "job_retry", job_id=job_id, attempt=attempts + 1, status="queued", legacy=legacy
    )
    return jsonify(status="queued", warnings=["legacy_recomputed"] if legacy else [])


@blueprint.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    """Request the cancellation of a queued or running job.

    Return HTTP 202 once requested: a queued job is cancelled at once, a
    running job stops at its next cancellation check. Return 200 when the job
    is already cancelled, 404 for an unknown job and 409 for a finished one.
    """
    services = get_services()
    outcome, status = services.store.request_cancel(job_id)
    if outcome == "missing":
        abort(404)
    if outcome == "already_cancelled":
        return jsonify(status="cancelled"), 200
    if outcome == "not_active":
        return jsonify(
            error="This job cannot be cancelled.", error_code="cancel_conflict"
        ), 409
    if outcome == "already_requested":
        return jsonify(status=status, cancel_requested=True), 202
    # The request is committed; let the running job see it without waiting for its next SQLite read.
    services.worker.notify_cancelled(job_id)
    event(
        "job_cancel_requested",
        job_id=job_id,
        status="cancelled" if outcome == "cancelled" else "cancelling",
    )
    return jsonify(status=status, cancel_requested=True), 202
