"""Run queued jobs one at a time in a background thread.

The queue lives in SQLite (see jobs.JobStore): the worker claims the oldest
queued job, runs it and records how it ended. A single worker keeps memory
and CPU use predictable on small machines.
"""

import logging
import threading
import time

from .config import ConfigurationError
from .errors import Cancelled, ProcessingError, WorkerStopping
from .observability import event, failure, job_context

# How long the worker sleeps when the queue looks empty. Creating, retrying
# or cancelling a job wakes it at once, so this is only a safety net.
IDLE_SECONDS = 6
# A running job rereads its cancellation flag from SQLite at most this often;
# requests made through the API reach it immediately (see notify_cancelled).
CANCEL_RECHECK_SECONDS = 1.0
# Leave time for the main-thread checkpoint within the usual container stop grace.
SHUTDOWN_TIMEOUT_SECONDS = 5.0


def record_failure(store, job_id, exc):
    """Record a failed job with a translatable code and a message safe to display.

    Only ProcessingError and ConfigurationError carry public text; any other
    exception is logged without its message and shown as a generic failure.
    """
    failure("job_failed", exc, job_id=job_id)
    if isinstance(exc, (ProcessingError, ConfigurationError)):
        code, message = exc.code, str(exc)
    else:
        code, message = (
            "analysis_failed",
            "Analysis failed. Check the PDF, configuration and local diagnostics.",
        )
    store.update_job(
        job_id,
        status="failed",
        stage="Error",
        phase_code="failed",
        error_code=code,
        error=message,
    )


class _CancelSignal:
    """In-process cancellation flag for the running job; SQLite stays authoritative."""

    def __init__(self):
        self.event = threading.Event()
        self.checked_at = float("-inf")


class Worker:
    """Claim queued jobs and run them one after another."""

    def __init__(self, store, run_job):
        """run_job(job_id, cancellation_requested) analyzes one claimed job."""
        self.store = store
        self.run_job = run_job
        self._wakeup = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        # Only the running job has a signal; cancel requests set it.
        self._signals: dict[str, _CancelSignal] = {}

    def start(self):
        """Run the queue in a daemon thread.

        stop() requests cooperative shutdown and waits for a bounded interval.
        A non-cooperative operation cannot hold the process open indefinitely;
        JobStore.initialize() recovers any interrupted job at the next start.
        """
        if self._thread is not None:
            raise RuntimeError("Worker has already been started.")
        self._thread = threading.Thread(
            target=self.run_forever, name="pdf-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout=SHUTDOWN_TIMEOUT_SECONDS):
        """Request shutdown and wait at most timeout seconds; never close another thread's connection."""
        self._stopping.set()
        self.wake()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        stopped = thread is None or not thread.is_alive()
        event(
            "worker_stopped" if stopped else "worker_shutdown_timeout",
            logging.INFO if stopped else logging.WARNING,
            status="stopped" if stopped else "pending",
        )
        return stopped

    def wake(self):
        """Look at the queue now instead of after the idle wait."""
        self._wakeup.set()

    def notify_cancelled(self, job_id):
        """Validate a committed cancellation against the captured execution signal."""
        signal = self._signals.get(job_id)
        # Capture before the SQLite read: a delayed notification must see a
        # still-persisted cancellation. If the attempt ends and another starts
        # after that read, setting this retained object cannot signal the new
        # attempt. Never look up the signal again after validating the flag.
        if signal is not None and self.store.read_cancellation(job_id):
            signal.event.set()
        self.wake()

    def cancellation_requested(self, job_id):
        """Answer frequent cooperative checks without opening SQLite each time.

        The model client checks once per streamed token. notify_cancelled()
        sets the running job's event immediately; SQLite is reread at most
        once per CANCEL_RECHECK_SECONDS so any other recorded request is
        still honored. Jobs not run by this worker always read SQLite.
        """
        if self._stopping.is_set():
            raise WorkerStopping()
        signal = self._signals.get(job_id)
        if signal is None:
            return self.store.read_cancellation(job_id)
        if not signal.event.is_set():
            now = time.monotonic()
            if now - signal.checked_at >= CANCEL_RECHECK_SECONDS:
                signal.checked_at = now
                if self.store.read_cancellation(job_id):
                    signal.event.set()
        return signal.event.is_set()

    def run_forever(self):
        """Process the queue until shutdown, then close the connection on its owning thread.

        After each job it immediately looks
        for the next one; when the queue is empty it sleeps until wake() or
        IDLE_SECONDS. Errors are logged and never stop the loop, and a wake()
        that arrives during a job is not lost because the queue is checked
        again before sleeping.
        """
        try:
            while not self._stopping.is_set():
                try:
                    job_id = self.store.claim_next_job()
                    if self._stopping.is_set():
                        break
                    if job_id:
                        self.run_claimed(job_id)
                        continue
                except Exception as exc:
                    failure("worker_database_error", exc)
                self._wakeup.wait(timeout=IDLE_SECONDS)
                self._wakeup.clear()
        finally:
            self.store.close(checkpoint=False)

    def run_claimed(self, job_id):
        """Run a job already marked ``running`` and record how it ended."""
        self._signals[job_id] = _CancelSignal()
        event("job_state", job_id=job_id, status="running", stage="starting")
        with job_context(job_id):
            try:
                self.run_job(job_id, self.cancellation_requested)
            except WorkerStopping:
                event("worker_job_interrupted", job_id=job_id, status="interrupted")
            except Cancelled:
                self.store.update_job(
                    job_id,
                    status="cancelled",
                    stage="Cancelled",
                    phase_code="cancelled",
                    error="",
                    error_code="cancelled",
                )
            except Exception as exc:
                # Keep the last preview to help diagnose the error.
                record_failure(self.store, job_id, exc)
            finally:
                self._signals.pop(job_id, None)
