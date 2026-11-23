"""Compare numeric matching work and local timings without a model.

Run from the repository root with a local preoptimization revision:
  .venv/bin/python tests/benchmark_numeric_checks.py --reference <revision>

Timings exclude evidence preparation and instrumentation. Each sample starts
with a cleared regex cache; equivalence includes dictionary and warning order.
"""

import argparse
import ast
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_numeric_filter import large_fixture

from document_processor import checks


def reference_checks(reference):
    source = subprocess.check_output(
        ["git", "show", f"{reference}:document_processor/checks.py"], text=True
    )
    method = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "numeric_checks"
    )
    namespace = dict(checks.__dict__)
    exec(
        compile(
            ast.Module(body=[method], type_ignores=[]),
            "reference_numeric_checks",
            "exec",
        ),
        namespace,
    )
    return namespace["numeric_checks"]


def benchmark(reference, repeat):
    answer, source, pages = large_fixture()
    assert len(source["comparisons"]) == 1200
    prepared = checks.prepare_evidence(source)
    expected = None
    match = checks._entity_in_line
    for name, function in (
        ("reference", reference_checks(reference)),
        ("working_tree", checks.numeric_checks),
    ):
        calls = 0

        def counted(*args):
            nonlocal calls
            calls += 1
            return match(*args)

        re.purge()
        with patch.dict(function.__globals__, {"_entity_in_line": counted}):
            result = function(answer, source, pages, prepared=prepared)
        serialized = json.dumps(result, ensure_ascii=False)
        if expected is None:
            expected = serialized
        assert serialized == expected
        samples = []
        for _ in range(repeat):
            re.purge()
            started = time.perf_counter()
            value = function(answer, source, pages, prepared=prepared)
            samples.append((time.perf_counter() - started) * 1000)
            assert json.dumps(value, ensure_ascii=False) == expected
        print(
            json.dumps(
                {
                    "variant": name,
                    "reference": reference,
                    "pages": len(pages),
                    "comparison_rows": len(source["comparisons"]),
                    "output_lines": len(answer.splitlines()),
                    "entity_match_calls": calls,
                    "checked": result["checked"],
                    "warnings": len(result["issues"]),
                    "exact_output_equal": True,
                    "elapsed_ms": [round(value, 3) for value in samples],
                    "median_ms": round(statistics.median(samples), 3),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--repeat", type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    benchmark(args.reference, args.repeat)
