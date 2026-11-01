"""Additive SQLite persistence and content-addressed artifact validation."""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import fingerprint


def now():
    """Return an unambiguous timestamp for persistent events."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def migrate(con):
    """Create V2 tables idempotently without rewriting historic job records."""
    columns = {row["name"] for row in con.execute("PRAGMA table_info(jobs)")}
    for name, declaration in {
        "phase_code": "TEXT NOT NULL DEFAULT ''", "phase_args": "TEXT NOT NULL DEFAULT '{}'",
        "error_code": "TEXT NOT NULL DEFAULT ''", "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "queued_at": "TEXT",
    }.items():
        if name not in columns:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
    con.execute("""CREATE TABLE IF NOT EXISTS job_configs (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS job_runs (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        duration_ms REAL, queue_ms REAL, provenance TEXT NOT NULL DEFAULT '{}')""")
    con.execute("""CREATE TABLE IF NOT EXISTS job_steps (
        id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        job_id TEXT NOT NULL, stage TEXT NOT NULL, step_key TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        metrics TEXT NOT NULL DEFAULT '{}')""")
    con.execute("CREATE INDEX IF NOT EXISTS job_runs_by_job ON job_runs(job_id, id)")
    con.execute("CREATE INDEX IF NOT EXISTS job_steps_by_job ON job_steps(job_id, id)")
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (2, ?)", (now(),))
    # Interrupted records remain visible as attempts, rather than looking active forever.
    con.execute("UPDATE job_steps SET status='interrupted', finished_at=? WHERE status='running'", (now(),))
    con.execute("UPDATE job_runs SET status='interrupted', finished_at=? WHERE status='running'", (now(),))


def save_config(con, job_id, config):
    """Insert a snapshot once; a retry must never replace its contents."""
    con.execute("INSERT INTO job_configs VALUES (?, ?, ?, ?)",
                (job_id, json.dumps(config, ensure_ascii=False, allow_nan=False), fingerprint(config), now()))


def load_config(connect, job_id):
    """Read and verify the private snapshot without exposing it through a row dump."""
    with connect() as con:
        row = con.execute("SELECT snapshot, fingerprint FROM job_configs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    config = json.loads(row["snapshot"])
    if fingerprint(config) != row["fingerprint"]:
        raise ValueError("Stored job configuration failed its integrity check.")
    return config


def file_hash(path):
    """Hash a file incrementally so an uploaded PDF is not duplicated in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Artifacts:
    """Validate artifacts and publish a manifest after each atomic file write.

    Legacy root files are never deleted. V2 work lives in a snapshot-specific
    directory, while the completed report is still published at its old path.
    A crash before manifest publication causes recomputation, never unsafe reuse.
    """

    def __init__(self, directory, config, atomic_json, atomic_text):
        self.directory = Path(directory)
        self.config_hash = fingerprint(config)
        self.root = self.directory / "artifacts" / self.config_hash
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "manifest.json"
        self.atomic_json, self.atomic_text = atomic_json, atomic_text
        self.manifest = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "version": 1, "config_hash": self.config_hash, "artifacts": {}, "identity": None}
        if self.manifest.get("config_hash") != self.config_hash or self.manifest.get("version") != 1:
            raise ValueError("Artifact manifest is incompatible with this job.")

    def bind_identity(self, identity):
        """Refuse known model/template changes before reusing generated notes."""
        previous = self.manifest.get("identity")
        if previous and any(previous.get(key) is not None and identity.get(key) != previous[key]
                            for key in previous):
            raise ValueError("The model or template changed. Start a new analysis.")
        self.manifest["identity"] = identity
        self.atomic_json(self.path, self.manifest)

    def read(self, name, dependencies):
        """Return verified text, or None when there is no reusable completed artifact."""
        entry = self.manifest["artifacts"].get(name)
        path = self.root / name
        if (not entry or entry.get("complete") is not True or entry.get("dependencies") != fingerprint(dependencies)
                or not path.is_file() or file_hash(path) != entry.get("sha256")):
            return None
        return path.read_text(encoding="utf-8")

    def write(self, name, text, dependencies):
        """Publish contents first, then the matching completed manifest entry."""
        path = self.root / name
        self.atomic_text(path, text)
        self.manifest["artifacts"][name] = {
            "dependencies": fingerprint(dependencies), "sha256": file_hash(path), "complete": True}
        self.atomic_json(self.path, self.manifest)


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

    def finish(self, status):
        """Close an attempt without conflating its duration with queue waiting."""
        with self.connect() as con:
            con.execute("UPDATE job_runs SET finished_at=?,status=?,duration_ms=? WHERE id=?",
                        (now(), status, (time.monotonic() - self.started) * 1000, self.run_id))


def metrics(connect, job_id, limit=200, before=None):
    """Return bounded public records; callers enforce job access before invoking this."""
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
