"""Store jobs in SQLite and their files under ``<data dir>/jobs/<job id>/``.

JobStore is the only code that reads or writes job records: the web routes,
the worker and the pipeline call its methods instead of writing SQL. Each
thread uses its own SQLite connection, kept open between calls (see
database.ThreadConnections), so the store is safe to share between the web
threads and the worker thread. Call close() when the process stops.
"""

import json
import logging
import os
import re
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from . import database
from .config import fingerprint, public_config
from .errors import Cancelled, ProcessingError
from .observability import event
from .previews import PreviewCache

# Files kept in each job directory. The report name predates the English
# code base and is kept so existing installations keep working.
SOURCE_FILE = "source.pdf"
REPORT_FILE = "rapport_final.md"
PREVIEW_FILE = "live_preview.json"

# Columns that update_job() may change; every other column is fixed at creation.
UPDATABLE_COLUMNS = {"status", "stage", "pages", "total_chunks", "done_chunks", "error", "progress_percent",
                     "phase_code", "phase_args", "error_code", "cancel_requested", "queued_at", "report_name"}
# Columns returned by the API; private configuration snapshots live in job_configs.
PUBLIC_COLUMNS = {"id", "filename", "question", "status", "stage", "pages", "total_chunks", "done_chunks",
                  "error", "created_at", "updated_at", "progress_percent", "phase_code", "phase_args",
                  "error_code", "cancel_requested", "queued_at", "report_name", "question_custom"}


def is_job_id(value):
    """Accept only server-generated job IDs (32 lowercase hex), never a path."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def utcnow():
    """Return the current UTC time to the second, as stored in job rows."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now():
    """Return the current UTC time to the millisecond, for measurements and previews."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class InvalidJobPath(LookupError):
    """The ID is not a server-generated job ID, or its directory is a symlink."""


class JobStore:
    """Read and write jobs, their configuration snapshots and their files."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "jobs.sqlite3"
        self._connections = database.ThreadConnections(self.db_path)
        self.previews = PreviewCache()

    def connect(self):
        """Open a transaction on the jobs database, on this thread's connection (see database.ThreadConnections).

        The block cannot contain another one, so do not call other JobStore methods inside it.
        """
        return self._connections.transaction()

    def close(self, *, checkpoint=True):
        """Close only the caller's connection, optionally attempting a WAL checkpoint.

        The worker uses checkpoint=False; the main thread makes the final
        attempt after waiting for it. Preserve WAL files if a checkpoint is
        incomplete or other writers may still commit (see ThreadConnections.close).
        """
        self._connections.close(checkpoint=checkpoint)

    def initialize(self):
        """Create the data directory and schema, then recover jobs cut short by a restart."""
        self.previews.clear()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        event("workspace_ready", status="available")
        database.initialize(self.db_path)
        with self.connect() as con:
            # Interrupted records remain visible as attempts, rather than looking active forever.
            con.execute("UPDATE job_steps SET status='interrupted', finished_at=? WHERE status='running'", (now(),))
            con.execute("UPDATE job_runs SET status='interrupted', finished_at=? WHERE status='running'", (now(),))
            interrupted = con.execute("SELECT id,cancel_requested FROM jobs WHERE status='running' "
                                      "OR (status='queued' AND cancel_requested=1)").fetchall()
            con.execute("UPDATE jobs SET status='cancelled', stage='Cancelled', phase_code='cancelled' "
                        "WHERE cancel_requested=1 AND status IN ('queued','running')")
            con.execute("UPDATE jobs SET status='queued', stage='Resuming after restart', queued_at=? "
                        "WHERE status='running'", (utcnow(),))
        for row in interrupted:
            event("job_recovered", job_id=row["id"], status="cancelled" if row["cancel_requested"] else "queued")
        event("sqlite_ready", status="initialized")

    # Job files ----------------------------------------------------------------

    def job_dir(self, job_id):
        """Return the job directory path without creating it.

        Raise InvalidJobPath for anything but a server-generated ID, or when
        the directory resolves elsewhere (a symlinked job root), so an ID can
        never be used as an arbitrary path.
        """
        if not is_job_id(job_id):
            event("invalid_job_id", logging.WARNING, status="rejected")
            raise InvalidJobPath(job_id)
        directory = self.data_dir / "jobs" / job_id
        if directory.resolve() != self.data_dir.resolve() / "jobs" / job_id:
            event("invalid_job_path", logging.WARNING, job_id=job_id, status="rejected")
            raise InvalidJobPath(job_id)
        return directory

    def report_available(self, job_id):
        """Cheaply tell the list whether a final report can be offered.

        Use lstat only: the jobs root and job directory must be real directories
        and the report a regular file, never a symlink. The report routes repeat
        the full path checks before serving it.
        """
        if not is_job_id(job_id):
            return False
        root = self.data_dir / "jobs"
        try:
            return (stat.S_ISDIR(os.lstat(root).st_mode) and stat.S_ISDIR(os.lstat(root / job_id).st_mode)
                    and stat.S_ISREG(os.lstat(root / job_id / REPORT_FILE).st_mode))
        except OSError:
            return False

    def remove_files(self, job_id):
        """Remove only an application's private UUID directory after DB commit.

        Retain the entire tree if ownership is uncertain (links, mounts, special
        files or an unsafe rmtree implementation). Never follow a stored filename
        or artifact path. A cleanup error is a warning, not a database rollback:
        some files may already be gone and history removal is still durable.
        Return None, "delete_files_unsafe" or "delete_files_failed".
        """
        if not is_job_id(job_id):
            return "delete_files_unsafe"
        directory = self.data_dir / "jobs" / job_id
        try:
            if directory.resolve() != self.data_dir.resolve() / "jobs" / job_id or directory.is_symlink():
                return "delete_files_unsafe"
            try:
                root_stat = directory.lstat()
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(root_stat.st_mode) or not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                return "delete_files_unsafe"

            def walk_error(error):
                raise error

            for root, dirs, files in os.walk(directory, followlinks=False, onerror=walk_error):
                for path in [Path(root), *(Path(root) / name for name in dirs + files)]:
                    info = path.lstat()
                    if (path.is_symlink() or path.is_mount() or info.st_dev != root_stat.st_dev
                            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                        return "delete_files_unsafe"
            shutil.rmtree(directory)
        except RuntimeError:
            # Path.resolve raises RuntimeError for symlink loops on Python 3.12.
            return "delete_files_unsafe"
        except OSError as exc:
            event("history_cleanup_failed", job_id=job_id, error_type=type(exc).__name__)
            return "delete_files_failed"
        return None

    # Job records --------------------------------------------------------------

    def serialize(self, row):
        """Convert a SQLite row into a consistent API dictionary.

        Present every ``done`` job at 100 percent, even if an older database
        still stores zero. This normalization does not rewrite the SQLite row.
        """
        job = {key: row[key] for key in row.keys() if key in PUBLIC_COLUMNS}
        job["phase_args"] = json.loads(job.get("phase_args") or "{}")
        # The migration also initialized older completed jobs to 0 percent.
        if job["status"] == "done":
            job["progress_percent"] = 100
            job["report_available"] = self.report_available(job["id"])
        return job

    def get_job(self, job_id):
        """Return the normalized job, or None; the private snapshot is not loaded."""
        with self.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return None if row is None else self.serialize(row)

    def get_live_job(self, job_id):
        """Read job state and the latest attempt together, without loading its configuration."""
        with self.connect() as con:
            row = con.execute("SELECT jobs.*, (SELECT MAX(id) FROM job_runs WHERE job_id=jobs.id) "
                              "AS latest_attempt_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        return (None, None) if row is None else (self.serialize(row), row["latest_attempt_id"])

    def list_jobs(self, limit=100):
        """Return the latest jobs with their public configuration, and whether any job is active.

        The activity flag covers the whole queue, including older jobs outside
        the list, so the browser keeps polling while any job is queued or running.
        """
        with self.connect() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
            has_active = con.execute("SELECT EXISTS(SELECT 1 FROM jobs WHERE status IN ('queued', 'running'))").fetchone()[0]
            # Reuse the fingerprint stored with each snapshot instead of rehashing it on every poll.
            configs = {row["job_id"]: (json.loads(row["snapshot"]), row["fingerprint"]) for row in con.execute(
                "SELECT job_id, snapshot, fingerprint FROM job_configs WHERE job_id IN "
                "(SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?)", (limit,))}
        jobs = [self.serialize(row) for row in rows]
        for job in jobs:
            if job["id"] in configs:
                job["configuration"] = public_config(*configs[job["id"]])
        return jobs, bool(has_active)

    def create_job(self, job_id, filename, question, question_custom, config):
        """Insert a queued job together with its immutable configuration snapshot."""
        created = utcnow()
        with self.connect() as con:
            con.execute("""INSERT INTO jobs (id, filename, question, status, stage, created_at, updated_at,
                           queued_at, question_custom) VALUES (?, ?, ?, 'queued', 'Queued', ?, ?, ?, ?)""",
                        (job_id, filename, question, created, created, created, question_custom))
            self._insert_config(con, job_id, config)

    def update_job(self, job_id, **values):
        """Store values and the update time for job_id.

        values maps UPDATABLE_COLUMNS to their new values. Becoming ``done``
        forces 100 percent and raises Cancelled instead if a cancellation was
        requested in the meantime. An unknown ID creates no row.
        """
        if not values:
            return
        if values.get("status") == "done":
            values["progress_percent"] = 100
        if set(values) - UPDATABLE_COLUMNS:
            raise ValueError("Unsupported job update field")
        values["updated_at"] = utcnow()
        parts = ", ".join(f"{k}=?" for k in values)
        with self.connect() as con:
            if values.get("status") == "done":
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row and row["cancel_requested"]:
                    raise Cancelled()
            con.execute(f"UPDATE jobs SET {parts} WHERE id=?", [*values.values(), job_id])
        if "status" in values:
            event("job_state", job_id=job_id, status=values["status"], stage=values.get("phase_code"))
        elif "progress_percent" in values:
            event("job_progress", job_id=job_id, stage=values.get("phase_code"),
                  progress_percent=values["progress_percent"])

    def set_report_name(self, job_id, name):
        """Remember a report download name computed for an older job."""
        with self.connect() as con:
            con.execute("UPDATE jobs SET report_name=? WHERE id=? AND report_name IS NULL", (name, job_id))

    def delete_job(self, job_id):
        """Delete a finished job's records; return None or an error code.

        Return "job_not_found", or "delete_active" while the job is queued,
        running or still recording its last attempt. Files are removed
        separately with remove_files(), only after this commit succeeds.
        Database errors propagate. BEGIN IMMEDIATE serializes this with
        retries and worker claims.
        """
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return "job_not_found"
            # A completed status can precede the worker's final metrics writes.
            finishing = con.execute(
                "SELECT 1 FROM job_runs WHERE job_id=? AND status='running' LIMIT 1", (job_id,)
            ).fetchone()
            if row["status"] not in ("done", "failed", "cancelled") or finishing:
                return "delete_active"
            con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self.previews.discard(job_id)
        return None

    # Queue --------------------------------------------------------------------

    def claim_next_job(self):
        """Mark the oldest queued job as running and return its ID, or None."""
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
            if row is None:
                return None
            con.execute("UPDATE jobs SET status='running', stage='Starting', updated_at=? WHERE id=?",
                        (utcnow(), row["id"]))
        return row["id"]

    def request_cancel(self, job_id):
        """Record a cancellation request and return (outcome, status).

        outcome is "missing", "already_cancelled", "not_active" (done or
        failed), "already_requested", "cancelled" (a queued job, cancelled at
        once) or "requested" (a running job, which stops at its next check).
        status is the job status after the call.
        """
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status,cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return "missing", None
            if row["status"] == "cancelled":
                return "already_cancelled", "cancelled"
            if row["status"] not in ("queued", "running"):
                return "not_active", row["status"]
            if row["cancel_requested"]:
                return "already_requested", row["status"]
            queued = row["status"] == "queued"
            con.execute("UPDATE jobs SET cancel_requested=1, status=?, phase_code=?, stage=?, updated_at=? WHERE id=?",
                        ("cancelled" if queued else "running", "cancelled" if queued else "cancelling",
                         "Cancelled" if queued else "Cancellation requested", utcnow(), job_id))
        return ("cancelled", "cancelled") if queued else ("requested", "running")

    def read_cancellation(self, job_id):
        """Read the persisted cancellation request; a deleted job counts as cancelled."""
        with self.connect() as con:
            row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return not row or bool(row["cancel_requested"])

    def requeue(self, job_id, default_max_attempts):
        """Queue a failed or cancelled job again; return (outcome, previous attempts).

        outcome is "queued", "limit" when the job used all its attempts, or
        "conflict" when the job is not failed or cancelled. Jobs saved before
        configuration snapshots use default_max_attempts.
        """
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            attempts = con.execute("SELECT COUNT(*) FROM job_runs WHERE job_id=?", (job_id,)).fetchone()[0]
            config_row = con.execute("SELECT snapshot FROM job_configs WHERE job_id=?", (job_id,)).fetchone()
            maximum = json.loads(config_row["snapshot"])["max_attempts"] if config_row else default_max_attempts
            if attempts >= maximum:
                return "limit", attempts
            changed = con.execute("""UPDATE jobs SET status='queued', error='', error_code='', stage='Retrying',
                phase_code='queued', cancel_requested=0, queued_at=?, updated_at=?
                WHERE id=? AND status IN ('failed','cancelled')""", (utcnow(), utcnow(), job_id)).rowcount
        if changed:
            self.previews.discard(job_id)
        return ("queued" if changed else "conflict"), attempts

    # Configuration snapshots and attempts -------------------------------------

    def save_config(self, job_id, config):
        """Save the snapshot of a job created before snapshots existed."""
        with self.connect() as con:
            self._insert_config(con, job_id, config)

    def _insert_config(self, con, job_id, config):
        """Insert a snapshot once, inside the caller's transaction; never replace it."""
        con.execute("INSERT INTO job_configs VALUES (?, ?, ?, ?)",
                    (job_id, json.dumps(config, ensure_ascii=False, allow_nan=False), fingerprint(config), now()))

    def load_config(self, job_id):
        """Read and verify the private snapshot; None for jobs saved before snapshots."""
        with self.connect() as con:
            row = con.execute("SELECT snapshot, fingerprint FROM job_configs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        config = json.loads(row["snapshot"])
        if fingerprint(config) != row["fingerprint"]:
            raise ProcessingError("config_integrity", "Stored job configuration failed its integrity check.")
        return config

    def attempt_count(self, job_id):
        """Return how many attempts were started for job_id."""
        with self.connect() as con:
            return con.execute("SELECT COUNT(*) FROM job_runs WHERE job_id=?", (job_id,)).fetchone()[0]

    def start_attempt(self, job_id, queued_at):
        """Record a new attempt and return its Recorder."""
        return Recorder(self.connect, job_id, queued_at)

    def metrics(self, job_id, limit=200, before=None):
        """Return the job's attempts and a page of its step measurements (see metrics())."""
        return metrics(self.connect, job_id, limit, before)


class Recorder:
    """Persist bounded step summaries, including unsuccessful generation attempts."""

    def __init__(self, connect, job_id, queued_at):
        self.connect, self.job_id = connect, job_id
        self.started = time.monotonic()
        try:
            queue_ms = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(queued_at)).total_seconds() * 1000)
        except (ValueError, TypeError):
            queue_ms = None
        with connect() as con:
            self.run_id = con.execute(
                "INSERT INTO job_runs (job_id, started_at, status, queue_ms) VALUES (?, ?, 'running', ?)",
                (job_id, now(), queue_ms)).lastrowid
        event("job_attempt_started", job_id=job_id, attempt_id=self.run_id, queue_ms=queue_ms)
        self.steps = {}

    def provenance(self, value):
        """Store sanitized backend observations separately from immutable options."""
        with self.connect() as con:
            con.execute("UPDATE job_runs SET provenance=? WHERE id=?", (json.dumps(value), self.run_id))

    def start(self, stage, key):
        """Record a step before doing work so crashes remain diagnosable."""
        with self.connect() as con:
            identifier = con.execute(
                "INSERT INTO job_steps (run_id,job_id,stage,step_key,started_at,status) VALUES (?,?,?,?,?,'running')",
                (self.run_id, self.job_id, stage, key, now())).lastrowid
        self.steps[identifier] = (stage, key)
        event("step_started", job_id=self.job_id, stage=stage, artifact=key, status="running")
        return identifier, time.monotonic()

    def end(self, handle, status, metrics=None):
        """Finish one step, using a monotonic clock for its application duration."""
        identifier, started = handle
        values = dict(metrics or {})
        values["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        values["duration_source"] = "application"
        with self.connect() as con:
            con.execute("UPDATE job_steps SET finished_at=?, status=?, metrics=? WHERE id=?",
                        (now(), status, json.dumps(values, allow_nan=False), identifier))
        stage, key = self.steps.pop(identifier, ("unknown", "unknown"))
        counts = {name: values[name] for name in
                  ("prompt_tokens", "output_tokens", "finish_reason", "error_code") if name in values}
        event("step_finished", job_id=self.job_id, stage=stage, artifact=key,
              status=status, duration_ms=values["duration_ms"], **counts)

    def finish(self, status):
        """Close an attempt without conflating its duration with queue waiting."""
        with self.connect() as con:
            con.execute("UPDATE job_runs SET finished_at=?,status=?,duration_ms=? WHERE id=?",
                        (now(), status, (time.monotonic() - self.started) * 1000, self.run_id))
        event("job_attempt_finished", job_id=self.job_id, attempt_id=self.run_id,
              status=status, duration_ms=round((time.monotonic() - self.started) * 1000, 1))


def metrics(connect, job_id, limit=200, before=None):
    """Return a job's measurements: they never contain document text or model output.

    The result holds the latest 100 attempts, the newest ``limit`` steps whose
    ID is below ``before``, step totals, and ``next_before`` to request the
    following page.
    """
    with connect() as con:
        runs = [dict(row) for row in con.execute(
            "SELECT * FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT 100", (job_id,))]
        rows = con.execute("SELECT * FROM job_steps WHERE job_id=? AND id<? ORDER BY id DESC LIMIT ?",
                           (job_id, before or 2**63 - 1, limit + 1)).fetchall()
        totals = dict(con.execute("""SELECT COUNT(*) AS steps,
            SUM(CASE WHEN status='reused' THEN 1 ELSE 0 END) AS reused_steps,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_steps
            FROM job_steps WHERE job_id=?""", (job_id,)).fetchone())
    for run in runs:
        run["provenance"] = json.loads(run["provenance"])
    steps = []
    for row in rows[:limit]:
        step = dict(row)
        step["metrics"] = json.loads(step["metrics"])
        steps.append(step)
    return {"runs": runs, "steps": steps, "summary": totals,
            "next_before": steps[-1]["id"] if len(rows) > limit else None}
