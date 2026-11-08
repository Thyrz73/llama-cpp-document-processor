"""Compare release components offline against an exported step-00 source tree.

Run once per source tree with the SAME locked Python environment, for example:
  python tests/benchmark_release.py --repository /tmp/step00 --label f55eb01
  python tests/benchmark_release.py --repository . --label release

All data is temporary. SQL and extraction timings are real; preview clocks and
tokenization responses are deterministic simulations. Idle memory belongs to
an actual Waitress/worker process with an empty queue. No inference is requested.
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch


def memory(pid):
    """Linux KiB; PSS distributes shared pages instead of counting each copy."""
    fields = {}
    path = Path(f"/proc/{pid}/smaps_rollup")
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("Rss", "Pss"):
                fields[key.lower() + "_kib"] = int(value.split()[0])
    return fields


def environment(directory):
    prompt = directory / "prompt.txt"
    prompt.write_text("Offline benchmark; no model generation.")
    return dict(
        os.environ,
        DATA_DIR=str(directory / "data"),
        SYSTEM_PROMPT_FILE=str(prompt),
        LLAMA_BASE_URL="http://127.0.0.1:1",
        LLAMA_API_KEY="",
        OMP_THREAD_LIMIT="1",
        PYTHONDONTWRITEBYTECODE="1",
    )


def idle_server(repository):
    with tempfile.TemporaryDirectory(prefix="release-idle-") as temporary:
        directory = Path(temporary)
        env = environment(directory)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env["APP_PORT"] = str(port)
        with tempfile.TemporaryFile(mode="w+") as log:
            server = subprocess.Popen(
                [sys.executable, "-u", str(repository / "app.py")],
                cwd=repository,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 15
                while True:
                    if server.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError("Idle server failed: " + log.read())
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/health", timeout=1
                        ) as response:
                            assert json.load(response)["status"] == "ok"
                        break
                    except OSError:
                        time.sleep(0.05)
                samples = []
                for _ in range(5):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/jobs", timeout=1
                    ) as response:
                        assert json.load(response)["jobs"] == []
                    samples.append(memory(server.pid))
                    time.sleep(0.05)
                return {
                    key: statistics.median(s[key] for s in samples)
                    for key in samples[0]
                }
            finally:
                server.terminate()
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def components(repository, directory):
    sys.path.insert(0, str(repository))
    import pymupdf

    import app
    from document_processor import config as job_config
    from document_processor import extraction, pipeline
    from document_processor.jobs import SOURCE_FILE, JobStore
    from document_processor.model_client import ModelClient

    logging.disable(logging.CRITICAL)
    subwords = re.compile(
        r"\[Page|\r\n|\n\n| [A-Za-z]{1,4}|[A-Za-z]{1,4}|\d{1,3}|[^\x00-\x7f]|.", re.S
    )

    class OfflineTokenizer(ModelClient):
        def __init__(self):
            super().__init__("http://unused.invalid", "", {}, "")
            self.method_calls = 0

        def tokenize(self, text):
            self.method_calls += 1
            return super().tokenize(text)

        def json_request(self, method, path, **kwargs):
            assert method == "POST" and path == "/tokenize"
            return {
                "tokens": list(range(len(subwords.findall(kwargs["json"]["content"]))))
            }

    pdf = directory / "sample.pdf"
    with pymupdf.open() as document:
        for number in range(120):
            document.new_page().insert_text(
                (40, 60),
                f"Scientific document {number}: observations, measurements and reliable results.",
            )
        document.save(pdf, deflate=True)
    store = JobStore(directory / "jobs-data")
    store.initialize()
    identifier = "a" * 32
    config = job_config.resolve(app.runtime_defaults())
    config["source_hash"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    config["system_prompt"] = "Offline fixture"
    config["templates"] = {}
    store.create_job(identifier, "sample.pdf", "Analyze", 1, config)
    job = store.job_dir(identifier)
    job.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf, job / SOURCE_FILE)
    try:
        with store.connect() as connection:
            connection.executemany(
                "INSERT INTO jobs (id,filename,question,status,created_at,updated_at) "
                "VALUES (?, 'fixture.pdf', 'Analyze', 'done', ?, '2026-01-01')",
                [
                    (
                        f"{index:032x}",
                        f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}",
                    )
                    for index in range(999)
                ],
            )
            connection.execute("UPDATE jobs SET status='done'")
            sql = {}
            for name, statement in (
                (
                    "list_100",
                    "SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT 100",
                ),
                (
                    "active",
                    "SELECT EXISTS(SELECT 1 FROM jobs WHERE status IN ('queued','running'))",
                ),
                (
                    "claim_select",
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1",
                ),
            ):
                connection.execute(statement).fetchall()
                timings = []
                for _ in range(200):
                    started = time.perf_counter_ns()
                    connection.execute(statement).fetchall()
                    timings.append((time.perf_counter_ns() - started) / 1000)
                sql[name] = {
                    "median_us": round(statistics.median(timings), 3),
                    "p95_us": round(sorted(timings)[189], 3),
                }
        model = Mock(tokenization_calls=0, tokenization_ms=0, tokenization_cache_hits=0)
        runtime = pipeline.Runtime(
            store, Mock(), Mock(return_value=model), Mock(), True, 1000
        )
        run = pipeline.JobRun(identifier, runtime)
        clock = 100.0
        with (
            patch.object(pipeline.time, "monotonic", side_effect=lambda: clock),
            patch.object(pipeline, "atomic_json", wraps=pipeline.atomic_json) as writes,
            patch.object(store, "update_job", wraps=store.update_job) as updates,
        ):
            for index in range(1, 121):
                clock = 100 + (index - 1) / 100
                run._extraction_progress(f"Extracting text: page {index}/120")
            progress = {
                "callbacks": 120,
                "sql_updates": updates.call_count,
                "preview_writes": writes.call_count,
            }
            writes.reset_mock()
            for index in range(101):
                clock = 200 + index * 0.8
                run._write_preview(
                    "sections",
                    active=True,
                    content="Measured output",
                    reasoning="",
                    metrics={},
                    started_at="fixed",
                )
            run._write_preview(
                "sections",
                active=False,
                content="Measured output",
                reasoning="",
                metrics={},
                started_at="fixed",
            )
            live = {"callbacks": 102, "preview_writes": writes.call_count}
        pages = [
            {
                "page": page,
                "text": "\n\n".join(
                    f"Observation {page}.{paragraph}: Scientific measurements record temperature 25 °C. "
                    "Methods compare calibrated instruments, numerical results and independent samples. "
                    "Résumé des résultats : hypothèse à vérifier, énergie et précision."
                    for paragraph in range(1, 4)
                ),
            }
            for page in range(1, 121)
        ]
        tokenizer = OfflineTokenizer()
        try:
            started = time.perf_counter()
            chunks = extraction.make_chunks(pages, tokenizer, 3000)
            tokenization = dict(
                method_calls=tokenizer.method_calls,
                endpoint_calls=tokenizer.tokenization_calls,
                chunks=len(chunks),
                seconds=time.perf_counter() - started,
            )
            assert all(len(subwords.findall(c["text"])) <= 3000 for c in chunks)
            assert "".join(
                part["text"] for chunk in chunks for part in chunk["parts"]
            ) == "".join(page["text"] for page in pages)
        finally:
            tokenizer.session.close()
        started = time.perf_counter()
        extracted = extraction.extract_pdf(pdf, config["extraction"])
        text_seconds = time.perf_counter() - started
        assert extracted[1:] == (120, [], [])
        with pymupdf.open() as source:
            source.new_page().insert_text(
                (50, 80),
                "Scientific measurements and reliable observations.",
                fontsize=16,
            )
            png = source[0].get_pixmap(dpi=150, alpha=False).tobytes("png")
        scan = directory / "scan.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_image(page.rect, stream=png)
            document.save(scan)
        started = time.perf_counter()
        scanned = extraction.extract_pdf(scan, config["extraction"])
        ocr_seconds = time.perf_counter() - started
        assert scanned[1:] == (1, [], [1])
        return dict(
            sql=sql,
            progress=progress,
            live=live,
            tokenizer=tokenization,
            extraction={"text_120_seconds": text_seconds, "ocr_1_seconds": ocr_seconds},
        )
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--label", default="working-tree")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    repository = args.repository.resolve()
    with tempfile.TemporaryDirectory(prefix="release-benchmark-") as temporary:
        directory = Path(temporary)
        if args.worker:
            with patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Network forbidden"),
            ):
                print(json.dumps(components(repository, directory)))
        else:
            samples = []
            for _ in range(args.repeat):
                output = subprocess.check_output(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--repository",
                        str(repository),
                    ],
                    cwd=repository,
                    env=environment(directory),
                    text=True,
                )
                row = json.loads(output)
                row["idle_server"] = idle_server(repository)
                samples.append(row)
            print(
                json.dumps(
                    dict(
                        label=args.label,
                        python=sys.version,
                        versions={
                            n: importlib.metadata.version(n)
                            for n in ("PyMuPDF", "pypdf", "Flask", "waitress")
                        },
                        sqlite=sqlite3.sqlite_version,
                        samples=samples,
                    ),
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
