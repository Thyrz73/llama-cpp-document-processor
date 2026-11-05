"""Plan controlled comparisons; execute only with the explicit --execute switch.

This client never changes llama.cpp settings or loads another model. Each trial
creates a new processor job. Exported results exclude PDFs, questions and report
text. Authorized local quality checklists can provide mechanical recall checks;
human review is still required for factual correctness and omissions.
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests


def leaves(value, prefix=""):
    """Flatten options so the plan can reject multi-variable comparisons."""
    result = {}
    for key, item in value.items():
        path = prefix + key
        if isinstance(item, dict):
            result.update(leaves(item, path + "."))
        else:
            result[path] = item
    return result


def validate_plan(variants, allow_combined=False):
    """Require named options and one changed input per baseline comparison."""
    if not isinstance(variants, list) or not 1 <= len(variants) <= 20:
        raise ValueError("Supply between 1 and 20 variants.")
    names = set()
    baseline = None
    for variant in variants:
        if not isinstance(variant, dict) or set(variant) != {"name", "options"}:
            raise ValueError("Each variant needs only name and options.")
        if (
            not isinstance(variant["name"], str)
            or not variant["name"]
            or len(variant["name"]) > 80
        ):
            raise ValueError("Variant names must be short, nonempty strings.")
        if variant["name"] in names or not isinstance(variant["options"], dict):
            raise ValueError(
                "Variant names must be unique and options must be objects."
            )
        names.add(variant["name"])
        current = leaves(variant["options"])
        if baseline is None:
            baseline = current
        changed = [
            key
            for key in baseline.keys() | current.keys()
            if baseline.get(key) != current.get(key)
        ]
        # Switching profiles changes several resolved budgets, despite being one UI field.
        if not allow_combined and (len(changed) > 1 or "profile" in changed):
            raise ValueError(
                "Change one parameter at a time; profile comparisons require --allow-combined."
            )
    return variants


def recall_checks(report, checklist):
    """Return mechanical match flags without exporting private expected facts."""
    return [
        {"id": item["id"], "matched": all(term in report for term in item["terms"])}
        for item in checklist
    ]


def execute_trial(session, base, pdf, question, variant, checklist, timeout):
    """Create a fresh job, observe it and collect all paginated step measurements."""
    with pdf.open("rb") as stream:
        response = session.post(
            base + "/api/jobs",
            files={"file": (pdf.name, stream, "application/pdf")},
            data={"question": question, "options": json.dumps(variant["options"])},
            timeout=(10, 120),
        )
    response.raise_for_status()
    created = response.json()
    identifier = created["id"]
    started = time.monotonic()
    try:
        while True:
            response = session.get(f"{base}/api/jobs/{identifier}", timeout=(10, 30))
            response.raise_for_status()
            job = response.json()
            if job["status"] not in ("queued", "running"):
                break
            if time.monotonic() - started > timeout:
                raise TimeoutError("Trial exceeded the configured client deadline.")
            time.sleep(2)
    except (KeyboardInterrupt, TimeoutError):
        # Execution consent includes stopping this benchmark-created job on timeout.
        session.post(
            f"{base}/api/jobs/{identifier}/cancel", timeout=(10, 30)
        ).raise_for_status()
        raise
    response = session.get(f"{base}/api/jobs/{identifier}/metrics", timeout=(10, 30))
    response.raise_for_status()
    measurements = response.json()
    before = measurements.get("next_before")
    while before:
        response = session.get(
            f"{base}/api/jobs/{identifier}/metrics",
            params={"before": before},
            timeout=(10, 30),
        )
        response.raise_for_status()
        page = response.json()
        measurements["steps"].extend(page["steps"])
        before = page.get("next_before")
    checks = []
    if job["status"] == "done" and checklist:
        response = session.get(f"{base}/api/jobs/{identifier}/report", timeout=(10, 30))
        response.raise_for_status()
        checks = recall_checks(response.content.decode("utf-8"), checklist)
    return {
        "variant": variant["name"],
        "job_id": identifier,
        "status": job["status"],
        "configuration": created.get("configuration"),
        "metrics": measurements,
        "mechanical_checks": checks,
        "manual_quality_review": "required",
    }


def main(argv=None):
    """Print a dry plan by default; never contact an endpoint without --execute."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--allow-combined", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:9932")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--question-file", type=Path)
    parser.add_argument("--checklist", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trial-timeout", type=int, default=86400)
    parser.add_argument(
        "--cache-condition", choices=("unknown", "warm", "cold"), default="unknown"
    )
    args = parser.parse_args(argv)
    variants = validate_plan(
        json.loads(args.variants.read_text(encoding="utf-8")), args.allow_combined
    )
    if not 1 <= args.repeat <= 10 or args.trial_timeout <= 0:
        parser.error("Use 1–10 repetitions and a positive trial timeout.")
    order = [
        variant
        for repeat in range(args.repeat)
        for variant in (variants if repeat % 2 == 0 else variants[::-1])
    ]
    if not args.execute:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "trials": [v["name"] for v in order],
                    "network_requests": 0,
                    "requires": "--execute --pdf --question-file --output",
                },
                indent=2,
            )
        )
        return 0
    if not args.pdf or not args.question_file or not args.output:
        parser.error("Execution requires --pdf, --question-file and --output.")
    if args.output.exists():
        parser.error(
            "Choose a new output file; previous results will not be overwritten."
        )
    question = args.question_file.read_text(encoding="utf-8").strip()
    if not 1 <= len(question) <= 1500:
        parser.error("The question must contain 1–1500 characters.")
    checklist = (
        json.loads(args.checklist.read_text(encoding="utf-8")) if args.checklist else []
    )
    if not isinstance(checklist, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not isinstance(item.get("terms"), list)
        or not item["terms"]
        or any(not isinstance(term, str) or not term for term in item["terms"])
        for item in checklist
    ):
        parser.error("Checklist entries need an id and a nonempty list of text terms.")
    # Output is private by default. Neither the question nor the PDF is exported.
    with args.pdf.open("rb") as source:
        source_hash = hashlib.file_digest(source, "sha256").hexdigest()
    result = {
        "schema_version": 1,
        "source_sha256": source_hash,
        "cache_condition": args.cache_condition,
        "combined_changes": args.allow_combined,
        "trials": [],
    }
    with requests.Session() as session:
        if os.getenv("PROCESSOR_BENCH_TOKEN"):
            session.headers["Authorization"] = (
                "Bearer " + os.environ["PROCESSOR_BENCH_TOKEN"]
            )
        for variant in order:
            trial = execute_trial(
                session,
                args.url.rstrip("/"),
                args.pdf,
                question,
                variant,
                checklist,
                args.trial_timeout,
            )
            result["trials"].append(trial)
            descriptor = os.open(
                args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            print(
                f"{variant['name']}: {trial['status']} ({len(result['trials'])}/{len(order)})"
            )
    summary = {}
    for variant in variants:
        durations = [
            sum(run["duration_ms"] or 0 for run in trial["metrics"]["runs"]) / 1000
            for trial in result["trials"]
            if trial["variant"] == variant["name"] and trial["status"] == "done"
        ]
        summary[variant["name"]] = {
            "successful_trials": len(durations),
            "median_seconds": statistics.median(durations) if durations else None,
            "min_seconds": min(durations) if durations else None,
            "max_seconds": max(durations) if durations else None,
        }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, requests.RequestException, TimeoutError) as exc:
        # Do not print HTTP exceptions: their URLs could contain credentials.
        print(
            f"Benchmark stopped ({type(exc).__name__}). Inspect local inputs and endpoint availability.",
            file=sys.stderr,
        )
        # Preserve the existing implicit exception context for CLI callers.
        raise SystemExit(1)  # noqa: B904
