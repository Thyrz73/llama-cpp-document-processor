"""Small structured events; never include document, prompt or model response text."""

import json
import logging
import os
import traceback
from pathlib import Path

log = logging.getLogger("document-processor")


def configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(level)
    logging.getLogger().setLevel(level)


def event(name, severity=logging.INFO, **fields):
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
