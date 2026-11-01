"""Inspect a processor data directory without starting workers or modifying files."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_processor.config import fingerprint
from document_processor.jobs import file_hash


def inspect(data):
    """Count historical and verifiable jobs while keeping private contents out of output."""
    database = data / "jobs.sqlite3"
    con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    result = {"jobs": 0, "historical_without_snapshot": 0, "snapshots_valid": 0,
              "artifacts_valid": 0, "integrity_errors": 0}
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = con.execute("SELECT id FROM jobs").fetchall()
        for row in rows:
            result["jobs"] += 1
            snapshot = con.execute("SELECT snapshot,fingerprint FROM job_configs WHERE job_id=?", (row["id"],)).fetchone() if "job_configs" in tables else None
            if snapshot is None:
                result["historical_without_snapshot"] += 1
                continue
            try:
                config = json.loads(snapshot["snapshot"])
                if fingerprint(config) != snapshot["fingerprint"]:
                    raise ValueError()
                result["snapshots_valid"] += 1
                root = data / "jobs" / row["id"]
                if file_hash(root / "source.pdf") != config["source_hash"]:
                    raise ValueError()
                artifact_root = root / "artifacts" / snapshot["fingerprint"]
                manifest_path = artifact_root / "manifest.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    if manifest.get("config_hash") != snapshot["fingerprint"] or manifest.get("version") != 1:
                        raise ValueError()
                    for name, entry in manifest["artifacts"].items():
                        path = artifact_root / name
                        if path.resolve().parent != artifact_root.resolve() or file_hash(path) != entry["sha256"]:
                            result["integrity_errors"] += 1
                        else:
                            result["artifacts_valid"] += 1
            except (ValueError, OSError, KeyError, TypeError):
                result["integrity_errors"] += 1
    finally:
        con.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="Path to a stopped processor's data directory or backup")
    args = parser.parse_args()
    try:
        result = inspect(args.data)
        print(json.dumps(result, indent=2))
        raise SystemExit(bool(result["integrity_errors"]))
    except (OSError, sqlite3.Error):
        print("Cannot inspect the selected data directory.", file=sys.stderr)
        raise SystemExit(1)
