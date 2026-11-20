"""Small file helpers shared by the job store, the artifacts and the pipeline."""

import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from .observability import current_job_id, event
from .report_text import REPORT_FILE


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


@contextmanager
def _directory(path: str | Path, parent: int | None = None) -> Iterator[int]:
    """Anchor each directory component without following symlinks."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _regular_file(
    parent: int, name: str, *, single_link: bool = True
) -> Iterator[BinaryIO]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
            raise ValueError("Unverifiable artifact file.")
        yield stream


def _file_state(stream: BinaryIO) -> tuple[int, ...]:
    info = os.fstat(stream.fileno())
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _copy_digest(
    source: BinaryIO,
    destination: BinaryIO | None,
    check_cancelled: Callable[[], None],
) -> str:
    """Hash the exact bytes copied, in one size-bounded pass over a stable file."""
    before = _file_state(source)
    remaining = before[2]
    digest = hashlib.sha256()
    while remaining:
        check_cancelled()
        content = source.read(min(1024 * 1024, remaining))
        if not content:
            raise ValueError("Report changed during archiving.")
        digest.update(content)
        if destination is not None:
            destination.write(content)
        remaining -= len(content)
    if source.read(1) or before != _file_state(source):
        raise ValueError("Report changed during archiving.")
    return digest.hexdigest()


def archive_report(directory: Path, check_cancelled: Callable[[], None]) -> None:
    """Archive an optional previous report as previous_reports/<raw SHA-256>.md.

    Reuse the artifact reader's descriptor validation. Only a missing source is
    optional; unsafe files, changed content and failed publication abort saving.
    Copy raw bytes once, preserving UTF-8 and line endings without decoding.
    Publish with link(), atomically and without replacing any existing entry;
    existing archives must match the source digest and are never rewritten.

    <data> and its ancestors must be trusted and stable. Open jobs, the job and
    previous_reports without following links; all archive operations use these
    descriptors, even if their pathname is subsequently replaced by a symlink.
    The opened directories must remain application-owned: an actor able to move
    them outside storage or tamper with private temporary entries is out of scope.
    The caller's subsequent report publication also requires stable job parents.
    No fsync/power-loss guarantee is added. A process crash may leave a private
    temporary, including a second link to a completely published archive.
    """
    with (
        _directory(directory.parent) as jobs,
        _directory(directory.name, jobs) as job,
        ExitStack() as sources,
    ):
        try:
            source = sources.enter_context(_regular_file(job, REPORT_FILE))
        except FileNotFoundError:
            return
        check_cancelled()
        with suppress(FileExistsError):
            os.mkdir("previous_reports", mode=0o700, dir_fd=job)
        with _directory("previous_reports", job) as archive:
            temporary = f".archive-{secrets.token_hex(16)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=archive,
            )
            with ExitStack() as cleanup:
                cleanup.callback(os.close, descriptor)
                cleanup.callback(os.unlink, temporary, dir_fd=archive)
                with os.fdopen(descriptor, "wb", closefd=False) as target:
                    digest = _copy_digest(source, target, check_cancelled)
                check_cancelled()
                name = digest + ".md"
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=archive,
                        dst_dir_fd=archive,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # Multiple links are allowed only for digest-verified, read-only
                    # existing archives: recover a crash between link and unlink.
                    with _regular_file(archive, name, single_link=False) as existing:
                        if _copy_digest(existing, None, check_cancelled) != digest:
                            raise ValueError(
                                "Existing archive digest mismatch."
                            ) from None
