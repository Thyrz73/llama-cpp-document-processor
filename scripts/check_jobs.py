"""Inspect a stopped processor data directory without modifying files or schema."""

import argparse
import json
import re
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_processor.artifacts import (
    MAX_REUSE_MANIFEST_BYTES,
    _is_hash,
    _read_bounded,
    _unique_object,
    _validate_name,
)
from document_processor.config import fingerprint
from document_processor.files import (
    _copy_digest,
    _directory,
    _file_state,
    _regular_file,
)


def _inspect_manifest(job, config_hash, result):
    """Absent work is normal; a present configuration directory needs a manifest."""
    with ExitStack() as stack:
        try:
            parent = stack.enter_context(_directory("artifacts", job))
        except FileNotFoundError:
            return
        try:
            directory = stack.enter_context(_directory(config_hash, parent))
        except FileNotFoundError:
            return
        manifest = json.loads(
            _read_bounded(directory, "manifest.json", MAX_REUSE_MANIFEST_BYTES),
            object_pairs_hook=_unique_object,
        )
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("version")) is not int
            or manifest["version"] != 1
            or manifest.get("config_hash") != config_hash
            or not isinstance(manifest.get("artifacts"), dict)
        ):
            raise ValueError("Invalid manifest.")
        for name, entry in manifest["artifacts"].items():
            try:
                _validate_name(name)
                if (
                    not isinstance(entry, dict)
                    or entry.get("complete") is not True
                    or not _is_hash(entry.get("dependencies"))
                    or not _is_hash(entry.get("sha256"))
                ):
                    raise ValueError("Invalid artifact entry.")
                with _regular_file(directory, name) as stream:
                    digest = _copy_digest(stream, None, lambda: None)
                if digest != entry["sha256"]:
                    raise ValueError("Artifact digest mismatch.")
                result["artifacts_valid"] += 1
            except (OSError, ValueError):
                result["integrity_errors"] += 1


def _inspect_snapshot(job, snapshot, source_hash, result):
    try:
        config = json.loads(snapshot["snapshot"], object_pairs_hook=_unique_object)
        if (
            not isinstance(config, dict)
            or not _is_hash(snapshot["fingerprint"])
            or fingerprint(config) != snapshot["fingerprint"]
        ):
            raise ValueError("Invalid configuration.")
        result["snapshots_valid"] += 1
        if config.get("source_hash") != source_hash:
            raise ValueError("Source mismatch.")
        _inspect_manifest(job, snapshot["fingerprint"], result)
    except (OSError, ValueError, TypeError, RecursionError):
        result["integrity_errors"] += 1


def _reference_errors(con, tables):
    """Check references explicitly, including old databases without declared FKs."""
    errors = 0
    for table in ("job_config_selections", "job_run_config_revisions"):
        if table not in tables:
            continue
        for row in con.execute(f"SELECT * FROM {table}"):
            revision = row["revision"]
            valid = (
                type(revision) is int
                and revision > 0
                and "job_config_revisions" in tables
                and "job_configs" in tables
                and con.execute(
                    "SELECT 1 FROM job_config_revisions r "
                    "JOIN job_configs c ON c.job_id=r.job_id "
                    "JOIN jobs j ON j.id=c.job_id WHERE r.job_id=? AND r.revision=?",
                    (row["job_id"], revision),
                ).fetchone()
                is not None
            )
            if table == "job_run_config_revisions":
                valid = (
                    valid
                    and "job_runs" in tables
                    and con.execute(
                        "SELECT 1 FROM job_runs WHERE id=? AND job_id=?",
                        (row["run_id"], row["job_id"]),
                    ).fetchone()
                    is not None
                )
            errors += not valid
    return errors


def inspect(data):
    """Count all stored snapshots/artifacts; never print private configuration data.

    The selected data directory and its ancestors must be trusted and stable,
    as with runtime safe file reads. Inspect a stopped directory or backup.
    Missing artifact directories are normal before processing; missing manifests
    inside existing configuration directories are reported as integrity errors.
    """
    data = Path(data).absolute()
    result = {
        "jobs": 0,
        "historical_without_snapshot": 0,
        "snapshots_valid": 0,
        "artifacts_valid": 0,
        "integrity_errors": 0,
    }
    with (
        _directory(data) as storage,
        _regular_file(storage, "jobs.sqlite3") as database,
    ):
        # mode=ro alone can create WAL/SHM files. Immutable reads cannot do so,
        # but ignore a WAL: refuse uncheckpointed data rather than inspect stale rows.
        try:
            with _regular_file(storage, "jobs.sqlite3-wal") as wal:
                if _file_state(wal)[2]:
                    raise ValueError("Inspect a checkpointed, stopped database.")
        except FileNotFoundError:
            pass
        before = _file_state(database)
        con = sqlite3.connect(
            (data / "jobs.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True
        )
        con.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            result["integrity_errors"] += _reference_errors(con, tables)
            for row in con.execute("SELECT id FROM jobs"):
                result["jobs"] += 1
                snapshots = []
                if "job_configs" in tables:
                    snapshots = con.execute(
                        "SELECT snapshot,fingerprint FROM job_configs WHERE job_id=?",
                        (row["id"],),
                    ).fetchall()
                if not snapshots:
                    result["historical_without_snapshot"] += 1
                if "job_config_revisions" in tables:
                    revisions = con.execute(
                        "SELECT revision,snapshot,fingerprint FROM job_config_revisions "
                        "WHERE job_id=? ORDER BY revision",
                        (row["id"],),
                    ).fetchall()
                    if revisions and not snapshots:
                        result["integrity_errors"] += 1
                    for revision in revisions:
                        if (
                            type(revision["revision"]) is not int
                            or revision["revision"] <= 0
                        ):
                            result["integrity_errors"] += 1
                    snapshots.extend(revisions)
                if not snapshots:
                    continue
                try:
                    if not isinstance(row["id"], str) or not re.fullmatch(
                        r"[0-9a-f]{32}", row["id"]
                    ):
                        raise ValueError("Invalid job identity.")
                    with (
                        _directory("jobs", storage) as jobs,
                        _directory(row["id"], jobs) as job,
                        _regular_file(job, "source.pdf") as source,
                    ):
                        source_hash = _copy_digest(source, None, lambda: None)
                        for snapshot in snapshots:
                            _inspect_snapshot(job, snapshot, source_hash, result)
                except (OSError, ValueError):
                    result["integrity_errors"] += 1
            # Foreign-key checking also catches orphaned original/revision rows.
            result["integrity_errors"] += sum(
                1 for _ in con.execute("PRAGMA foreign_key_check")
            )
        finally:
            con.close()
        if before != _file_state(database):
            raise ValueError("Database changed during inspection.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data", type=Path, help="Path to a stopped processor's data directory or backup"
    )
    args = parser.parse_args()
    try:
        result = inspect(args.data)
        print(json.dumps(result, indent=2))
        raise SystemExit(bool(result["integrity_errors"]))
    except (OSError, ValueError, sqlite3.Error):
        print("Cannot inspect the selected data directory.", file=sys.stderr)
        raise SystemExit(1) from None
