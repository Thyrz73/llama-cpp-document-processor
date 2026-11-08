"""Compare SQLite with/without v4 indexes on disposable 100/1000-job histories.

Run: .venv/bin/python tests/benchmark_database_indexes.py
SQL is captured from the actual JobStore methods; no retained application data is opened.
VM instruction counting is separate from timing. Timings are local warm reads,
not HTTP latency; insertion samples use the real create_job transaction.
"""

import json
import logging
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_database_indexes import capture_queries, plans, populate

from document_processor import database
from document_processor.jobs import JobStore


def named_queries(statements):
    result = {}
    for sql in statements:
        if sql.startswith("SELECT jobs.*"):
            result["list_order"] = sql
        elif sql.startswith("SELECT EXISTS"):
            result["has_active_jobs"] = sql
        elif sql.startswith("SELECT id FROM jobs WHERE status='queued'"):
            result["claim_select"] = sql
        elif sql.startswith("SELECT * FROM jobs WHERE id="):
            result["get_job"] = sql
        elif sql.startswith("SELECT * FROM job_runs"):
            result["metrics_runs"] = sql
        elif sql.startswith("SELECT * FROM job_steps"):
            result["metrics_steps"] = sql
        elif sql.startswith("SELECT COUNT(*) AS steps"):
            result["metrics_totals"] = sql
        elif sql.startswith("SELECT 1 FROM job_runs"):
            result["delete_finishing_guard"] = sql
        elif sql.startswith("SELECT status FROM jobs"):
            result["delete_status_guard"] = sql
        elif sql.startswith("DELETE FROM jobs"):
            result["delete_plan_only"] = sql
    return result


def summary(samples):
    return {
        "samples": len(samples),
        "median_us": round(statistics.median(samples), 3),
        "p95_us": round(sorted(samples)[int(len(samples) * 0.95)], 3),
    }


def benchmark():
    logging.disable(logging.CRITICAL)
    # Keep later schema available to current JobStore; only v4 indexes differ.
    without_indexes = tuple(item for item in database.MIGRATIONS if item[0] != 4)
    for count in (100, 1000):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stores, data, expected = {}, {}, {}
            try:
                # Capture real SQL on a separate disposable fixture; claim/delete may mutate it.
                capture = JobStore(root / "capture")
                capture.data_dir.mkdir(parents=True)
                with patch.object(database, "MIGRATIONS", without_indexes):
                    database.initialize(capture.db_path)
                populate(capture, count)
                try:
                    queries = named_queries(capture_queries(capture))
                finally:
                    capture.close()
                for variant in ("v3", "v4"):
                    store = stores[variant] = JobStore(root / variant)
                    store.data_dir.mkdir(parents=True)
                    with patch.object(database, "MIGRATIONS", without_indexes):
                        database.initialize(store.db_path)
                    populate(store, count)
                    migration_ms = None
                    if variant == "v4":
                        started = time.perf_counter_ns()
                        database.initialize(store.db_path)
                        migration_ms = (time.perf_counter_ns() - started) / 1_000_000
                    query_plans = plans(store, list(queries.values()))
                    results, work = {}, {}
                    with store.connect() as con:
                        size = (
                            con.execute("PRAGMA page_count").fetchone()[0]
                            * con.execute("PRAGMA page_size").fetchone()[0]
                        )
                        for name, sql in queries.items():
                            if name == "delete_plan_only":
                                continue
                            # Prime the statement cache before counting VM operations.
                            con.execute(sql).fetchall()
                            instructions = 0

                            def progress():
                                nonlocal instructions
                                instructions += 1
                                return 0

                            con.set_progress_handler(progress, 1)
                            try:
                                results[name] = [
                                    tuple(row) for row in con.execute(sql).fetchall()
                                ]
                            finally:
                                con.set_progress_handler(None, 0)
                            work[name] = instructions
                    if not expected:
                        expected = results
                    assert results == expected
                    store.list_jobs(
                        100
                    )  # Warm public controls and filesystem probes before timing.
                    data[variant] = {
                        "jobs": count,
                        "variant": variant,
                        "sqlite_version": sqlite3.sqlite_version,
                        "migration_ms": migration_ms,
                        "database_bytes_before_insert_samples": size,
                        "plans": {
                            name: query_plans[sql] for name, sql in queries.items()
                        },
                        "read_vm_steps": work,
                        "read_results_equal": results == expected,
                        "sql_samples": {name: [] for name in work},
                        "method_samples": {
                            name: [] for name in ("list_jobs", "get_job", "metrics")
                        },
                    }
                # Alternate variants over five rounds to reduce order/warmup bias.
                for round_number in range(5):
                    for variant in (
                        ("v3", "v4") if round_number % 2 == 0 else ("v4", "v3")
                    ):
                        store = stores[variant]
                        with store.connect() as con:
                            for name in data[variant]["sql_samples"]:
                                for _ in range(50):
                                    started = time.perf_counter_ns()
                                    con.execute(queries[name]).fetchall()
                                    data[variant]["sql_samples"][name].append(
                                        (time.perf_counter_ns() - started) / 1000
                                    )
                        for name in data[variant]["method_samples"]:
                            method = getattr(store, name)
                            args = (100,) if name == "list_jobs" else (f"{1:032x}",)
                            for _ in range(50):
                                started = time.perf_counter_ns()
                                method(*args)
                                data[variant]["method_samples"][name].append(
                                    (time.perf_counter_ns() - started) / 1000
                                )
                for variant, store in stores.items():
                    samples = []
                    for number in range(1_000_000, 1_000_200):
                        started = time.perf_counter_ns()
                        store.create_job(
                            f"{number:032x}",
                            "insert.pdf",
                            "Analyze",
                            1,
                            {
                                "profile": "low",
                                "max_attempts": 3,
                                "system_prompt": "Synthetic private prompt",
                            },
                        )
                        samples.append((time.perf_counter_ns() - started) / 1000)
                    data[variant]["create_job"] = summary(samples)
                    for group in ("sql_samples", "method_samples"):
                        data[variant][group] = {
                            name: summary(values)
                            for name, values in data[variant][group].items()
                        }
                    print(json.dumps(data[variant]))
            finally:
                for store in stores.values():
                    store.close()


if __name__ == "__main__":
    benchmark()
