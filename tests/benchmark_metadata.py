"""Measure cumulative JSON serialization offline; no filesystem/model work.

Run from the repository root with PYTHONPATH=.:
  python tests/benchmark_metadata.py
  python tests/benchmark_metadata.py --compact

Every step publishes one growing manifest and one growing quality ledger.
Times include serialization and UTF-8 byte counting, excluding fixture creation
and filesystem I/O. Byte counts are deterministic; timings depend on the host.
"""

import argparse
import copy
import hashlib
import json
import time
from unittest.mock import patch

from document_processor import files, reliability


def documents(count):
    evidence = reliability.build_evidence(
        [{"page": 1, "text": "École : 12,4 → 10,8 MWh"}], 1
    )
    check = reliability.audit("École : −2,6 MWh [Page 1]", evidence, [1])
    check.update(input_pages=[1], source_sections=[0])
    return [
        (
            f"analysis_{index:04d}.md",
            {
                "dependencies": hashlib.sha256(f"input {index}".encode()).hexdigest(),
                "sha256": hashlib.sha256(f"output {index}".encode()).hexdigest(),
                "complete": True,
            },
            {**copy.deepcopy(check), "source_sections": [index]},
        )
        for index in range(count)
    ]


def measure(count, compact):
    entries = documents(count)
    manifest = {
        "version": 1,
        "config_hash": "a" * 64,
        "artifacts": {},
        "identity": {
            "model_id_hash": "b" * 64,
            "template_hash": "c" * 64,
            "engine_sampling_hash": "d" * 64,
            "version": "synthetic",
        },
    }
    quality = {"version": reliability.VERSION, "steps": {}}
    totals = {"manifest.json": 0, "quality_checks.json": 0}
    last = {}
    calls = 0

    def capture(path, text):
        nonlocal calls
        calls += 1
        totals[path] += len(text.encode("utf-8"))
        last[path] = text

    elapsed = 0.0
    options = {"compact": True} if compact else {}
    with patch.object(files, "atomic_text", new=capture):
        for name, artifact, check in entries:
            manifest["artifacts"][name] = artifact
            quality["steps"][name] = check
            started = time.perf_counter()
            files.atomic_json("manifest.json", manifest, **options)
            files.atomic_json("quality_checks.json", quality, **options)
            elapsed += time.perf_counter() - started
    assert json.loads(last["manifest.json"]) == manifest
    assert json.loads(last["quality_checks.json"]) == quality
    return {
        "entries": count,
        "publications": calls,
        "manifest_bytes": totals["manifest.json"],
        "quality_bytes": totals["quality_checks.json"],
        "total_bytes": sum(totals.values()),
        "serialization_seconds": round(elapsed, 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "compact": args.compact,
                "measurements": [
                    measure(count, args.compact) for count in (100, 500, 1000)
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
