"""Small file helpers shared by the job store, the artifacts and the pipeline."""

import hashlib
import json
import logging
import os
from pathlib import Path

from .observability import current_job_id, event


def atomic_text(path, text):
    """Atomically replace a file with UTF-8 text through a temporary file.

    path is a path or Path object whose parent must already exist. Write to
    a neighboring file ending in .tmp first, then move it with os.replace so
    readers never see partial content. Return no value and propagate I/O
    errors.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        event("artifact_write_failed", logging.ERROR, artifact=path.name,
              job_id=current_job_id(), error_type=type(exc).__name__)
        raise


def atomic_json(path, data):
    """Atomically write data as UTF-8 JSON to path.

    data must be JSON serializable. Preserve Unicode characters, then
    delegate the write to atomic_text; the parent directory must already
    exist.
    """
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def file_hash(path):
    """Hash a file incrementally so an uploaded PDF is not duplicated in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
