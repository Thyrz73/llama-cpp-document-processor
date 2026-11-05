"""Reproduce chunking request counts without a model server.

Run from the repository root:
  .venv/bin/python tests/benchmark_chunking.py --reference 6a64d238967624da21c736af59682b1ae5e257f3

Both implementations use identical fixtures and the real ModelClient's tokenizer
cache against the offline subword endpoint from test_chunking. Counts describe
simulated endpoint requests, not network latency or a real model vocabulary.
"""

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_processor import extraction
from document_processor.profiles import PROFILES
from test_chunking import SimulatedTokenizer, fixture_pages, subword_count


def reference_chunker(reference):
    source = subprocess.check_output(
        ["git", "show", f"{reference}:document_processor/extraction.py"], text=True)
    method = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                  and node.name == "make_chunks")
    namespace = dict(extraction.__dict__)
    exec(compile(ast.Module(body=[method], type_ignores=[]), "reference_make_chunks", "exec"), namespace)
    return namespace["make_chunks"]


def benchmark(reference):
    before = reference_chunker(reference)
    for page_count in (1, 3, 120):
        pages = fixture_pages(page_count)
        for profile in ("low", "ultra"):
            for name, chunker in (("reference", before), ("working_tree", extraction.make_chunks)):
                model = SimulatedTokenizer()
                try:
                    started = time.perf_counter()
                    chunks = chunker(pages, model, PROFILES[profile]["chunk_tokens"])
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    # Validate separately: do not include verification in measured request counts.
                    assert all(subword_count(chunk["text"]) <= PROFILES[profile]["chunk_tokens"] for chunk in chunks)
                    assert [(part["page"], char) for chunk in chunks for part in chunk["parts"] for char in part["text"]] == [
                        (page["page"], char) for page in pages for char in page["text"]]
                    print(json.dumps({"variant": name, "pages": page_count, "profile": profile,
                                      "budget": PROFILES[profile]["chunk_tokens"], "chunks": len(chunks),
                                      "tokenize_calls": model.method_calls, "tokenize_characters": model.method_characters,
                                      "endpoint_calls": model.tokenization_calls, "endpoint_characters": model.http_characters,
                                      "elapsed_ms": round(elapsed_ms, 3)}))
                finally:
                    model.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, help="Local Git revision with the previous make_chunks")
    benchmark(parser.parse_args().reference)
