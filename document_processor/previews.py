"""Bounded, process-local live previews; the JSON checkpoint remains the fallback."""

import copy
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path

# One worker is active today; keep a small fixed budget even with more producers.
MAX_CACHED_PREVIEWS = 16
PREVIEW_TTL_SECONDS = 300.0
CHECKPOINT_SECONDS = 20.0
# Two channels of up to 50,000 characters, including JSON escapes and metadata.
MAX_CHECKPOINT_BYTES = 1024 * 1024
PREVIEW_FIELDS = {
    "phase",
    "phase_code",
    "phase_args",
    "updated_at",
    "content",
    "reasoning",
    "content_chars",
    "reasoning_chars",
    "active",
    "metrics",
    "started_at",
    "elapsed_seconds",
    "_attempt_id",
}


def read_checkpoint(path: Path) -> dict | None:
    """Read a bounded UTF-8 preview, or return None for an unsafe/invalid file.

    The caller supplies <data>/jobs/<validated job id>/live_preview.json.
    <data> and its ancestors must be trusted and stable. Anchor jobs and the job
    directory without following links, then validate the actual opened file.
    Replacement after opening cannot redirect the read. Hard links and special
    files are rejected; in-place changes observed during reading are rejected.
    This does not authenticate content written by an actor with storage access.
    """
    try:
        with ExitStack() as stack:
            parent = None
            for component in (path.parent.parent, path.parent.name):
                parent = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
                stack.callback(os.close, parent)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size > MAX_CHECKPOINT_BYTES
                ):
                    return None
                content = stream.read(before.st_size + 1)
                after = os.fstat(stream.fileno())
                if (
                    len(content) != before.st_size
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                    or after.st_nlink != 1
                ):
                    return None
        payload = json.loads(content.decode("utf-8"))
        if isinstance(payload, dict):
            return {
                key: value for key, value in payload.items() if key in PREVIEW_FIELDS
            }
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass
    return None


class PreviewCache:
    """Own snapshots, never references to mutable model metrics or HTTP responses.

    Only pipeline preview fields belong here, with content/reasoning bounded by
    preview_chars. Expiration is based on publication, never on polling. Disk
    reads are deliberately not cached: a different process may publish them.
    """

    def __init__(self, max_entries=MAX_CACHED_PREVIEWS, ttl=PREVIEW_TTL_SECONDS):
        self.max_entries = max_entries
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def _expire(self, current):
        for key, (published, _) in list(self._entries.items()):
            if current - published >= self.ttl:
                del self._entries[key]

    def put(self, job_id, payload, preview_chars):
        """Publish a bounded independent snapshot, evicting the oldest publication."""
        bounded = copy.deepcopy(
            {key: value for key, value in payload.items() if key in PREVIEW_FIELDS}
        )
        for field in ("content", "reasoning"):
            bounded[field] = (
                bounded.get(field, "")[-preview_chars:] if preview_chars > 0 else ""
            )
        with self._lock:
            current = time.monotonic()
            self._expire(current)
            self._entries[job_id] = (current, bounded)
            self._entries.move_to_end(job_id)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, job_id):
        """Return a copy, so reasoning redaction and readers cannot mutate the cache."""
        with self._lock:
            self._expire(time.monotonic())
            entry = self._entries.get(job_id)
            return copy.deepcopy(entry[1]) if entry else None

    def discard(self, job_id):
        """Release a deleted, retried or finished job's snapshot."""
        with self._lock:
            self._entries.pop(job_id, None)

    def clear(self):
        """Drop process-local observations during startup recovery."""
        with self._lock:
            self._entries.clear()
