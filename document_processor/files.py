"""Small file helpers shared by the job store, the artifacts and the pipeline."""

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from .observability import current_job_id, event


def atomic_text(path, text):
    """Replace path with UTF-8 text atomically: readers never see a partial file.

    A private, exclusively created temporary in the destination directory is
    closed before replacement. Its context owns cleanup on exceptions; existing
    temporary names are never opened or removed. Python 3.12+ keeps cleanup
    active after close with delete_on_close=False.

    The destination's parents must exist and remain application-controlled and
    stable during creation, replacement and cleanup. This does not protect
    against an actor who can concurrently rename those directories or entries.
    Replacement is atomic on the same filesystem, without a power-loss fsync
    guarantee. I/O errors are logged, then raised.
    """
    path = Path(path)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete_on_close=False,
        ) as tmp:
            tmp.write(text)
            tmp.close()
            os.replace(tmp.name, path)
    except OSError as exc:
        event(
            "artifact_write_failed",
            logging.ERROR,
            artifact=path.name,
            job_id=current_job_id(),
            error_type=type(exc).__name__,
        )
        raise


def atomic_json(path, data):
    """Write data to path as UTF-8 JSON, atomically (see atomic_text)."""
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def file_hash(path):
    """Hash a file incrementally so an uploaded PDF is not duplicated in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
