"""Bounded, process-local live previews; the JSON checkpoint remains the fallback."""

import copy
import threading
import time
from collections import OrderedDict

# One worker is active today; keep a small fixed budget even with more producers.
MAX_CACHED_PREVIEWS = 16
PREVIEW_TTL_SECONDS = 300.0
CHECKPOINT_SECONDS = 20.0
PREVIEW_FIELDS = {"phase", "phase_code", "phase_args", "updated_at", "content", "reasoning",
                  "content_chars", "reasoning_chars", "active", "metrics", "started_at",
                  "elapsed_seconds", "_attempt_id"}


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
        bounded = copy.deepcopy({key: value for key, value in payload.items() if key in PREVIEW_FIELDS})
        for field in ("content", "reasoning"):
            bounded[field] = bounded.get(field, "")[-preview_chars:] if preview_chars > 0 else ""
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
