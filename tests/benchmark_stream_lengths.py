"""Measure stored-fragment visits in real generation code with an offline SSE stream.

Run from the repository root:
  .venv/bin/python tests/benchmark_stream_lengths.py --reference <commit-before-stream-change>

The iterator instrumentation measures accounting work, not model/network latency.
"""

import argparse
import ast
import json
import subprocess
import sys
import time
from pathlib import Path
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_stream_metrics import CountingParts

from document_processor import model_client


def benchmark(reference):
    source = subprocess.check_output(
        ["git", "show", f"{reference}:document_processor/model_client.py"], text=True
    )
    body = ast.parse(source).body
    splitter = next(
        node
        for node in body
        if isinstance(node, ast.ClassDef) and node.name == "ThinkingSplitter"
    )
    client_class = next(
        node
        for node in body
        if isinstance(node, ast.ClassDef) and node.name == "ModelClient"
    )
    generate = next(
        node
        for node in client_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate"
    )
    namespace = dict(model_client.__dict__)
    exec(
        compile(
            ast.Module(body=[splitter, generate], type_ignores=[]),
            "reference_stream",
            "exec",
        ),
        namespace,
    )
    previous_splitter = namespace["ThinkingSplitter"]
    for count in (1000, 10000):
        expected = None
        # Keep callback signatures; these fixtures are consumed within each iteration.
        for variant in ("reference", "working_tree"):
            base = (
                previous_splitter
                if variant == "reference"
                else model_client.ThinkingSplitter
            )
            instances = []

            class TrackedSplitter(base):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.content_parts = CountingParts()
                    instances.append(self)  # noqa: B023

            client = model_client.ModelClient("http://unused.invalid", "", {}, "")
            client.fits = lambda *args: (True, 10, "estimated_template", 384)
            client.model = lambda: "fake"
            line = "data: " + json.dumps(
                {"choices": [{"delta": {"content": "abcdefgh"}}]}
            )

            def lines(payload):
                # Keep callback signatures; these fixtures are consumed within each iteration.
                for _ in range(count):  # noqa: B023
                    client.check_cancelled()  # noqa: B023
                    yield line  # noqa: B023
                yield "data: [DONE]"

            client.stream_lines = lines
            if variant == "reference":
                namespace["ThinkingSplitter"] = TrackedSplitter
                client.generate = MethodType(namespace["generate"], client)
            try:
                with patch.object(model_client, "ThinkingSplitter", TrackedSplitter):
                    started = time.perf_counter()
                    result = client.generate("instructions", "source", 100)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                values = (
                    result.content,
                    {
                        key: value
                        for key, value in result.metrics.items()
                        if key
                        not in (
                            "request_started_at",
                            "request_ms",
                            "first_event_ms",
                            "first_content_ms",
                        )
                    },
                )
                if expected is None:
                    expected = values
                assert values == expected
                print(
                    json.dumps(
                        {
                            "variant": variant,
                            "fragments": count,
                            "stored_fragment_visits": instances[0].content_parts.visits,
                            "content_chars": result.metrics["content_chars"],
                            "reasoning_chars": result.metrics["reasoning_chars"],
                            "output_and_non_timing_metrics_equal": values == expected,
                            "instrumented_ms": round(elapsed_ms, 3),
                        }
                    )
                )
            finally:
                client.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    benchmark(parser.parse_args().reference)
