"""Small structured events; never include document, prompt or model response text."""

import json
import logging
import os
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("document-processor")
# Job processed by the current thread, so helpers can label their log events.
_job = threading.local()


def configure_logging():
    """Apply LOG_LEVEL (default INFO) to the application and root loggers."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(level)
    logging.getLogger().setLevel(level)


@contextmanager
def job_context(job_id):
    """Mark job_id as the job processed by this thread until the block exits."""
    _job.job_id = job_id
    try:
        yield
    finally:
        _job.job_id = None


def current_job_id():
    """Return the job processed by this thread, or None outside the worker."""
    return getattr(_job, "job_id", None)


def event(name, severity=logging.INFO, **fields):
    """Log one line: the event name followed by JSON-encoded key=value fields."""
    if log.isEnabledFor(severity):
        log.log(severity, "%s %s", name, " ".join(
            f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in fields.items()))


def failure(name, exc, **fields):
    """Record all traceback frames without source lines, locals or private messages."""
    code = getattr(exc, "code", "application_error")
    fields.update(error_code=code, error_type=type(exc).__name__)
    context = " ".join(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in fields.items())
    frames = "\n".join(f'  File "{Path(frame.f_code.co_filename).name}", line {line}, in {frame.f_code.co_name}'
                       for frame, line in traceback.walk_tb(exc.__traceback__))
    log.error("%s %s\nTraceback (most recent call last):\n%s\n%s: %s (private details omitted)",
              name, context, frames, type(exc).__name__, code)
