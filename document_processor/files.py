"""Small file helpers shared by the job store, the artifacts and the pipeline."""

import hashlib
import json
import logging
import os
from pathlib import Path

from .observability import current_job_id, event


def atomic_text(path, text):
    """Replace path with UTF-8 text atomically: readers never see a partial file.

    The text is written to a neighbouring .tmp file, then moved over path. The
    parent directory must exist. I/O errors are logged, then raised.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        event(
            "artifact_write_failed",
            logging.ERROR,
            artifact=path.name,
            job_id=current_job_id(),
            error_type=type(exc).__name__,
        )
        raise


def atomic_json(path, data, *, compact=False):
    """Write UTF-8 JSON atomically; opt in to compact, sorted, finite metadata."""
    if compact:
        text = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_text(path, text)


def file_hash(path):
    """Hash a file incrementally so an uploaded PDF is not duplicated in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
