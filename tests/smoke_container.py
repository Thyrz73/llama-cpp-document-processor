"""Smoke-test a built runtime image offline, including migration and real OCR.

CI feeds this file to `docker run -i ... python -` from /app. It can also run
from the repository root using the locked Python environment. It uses only
disposable data, no external network, and never queues or generates an analysis.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pymupdf


def main():
    root = Path.cwd()
    sys.path.insert(0, str(root))
    from document_processor import __version__, database
    from document_processor.extraction import extract_pdf

    with tempfile.TemporaryDirectory(prefix="processor-smoke-") as temporary:
        directory = Path(temporary)
        data = directory / "data"
        identifier = "a" * 32
        job = data / "jobs" / identifier
        previous = job / "previous_reports" / "retained.md"
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"Previous report: keep this file.\n")
        report = "# Résultats / Results\n\nMesures vérifiées : 25 °C.\n".encode()
        (job / "rapport_final.md").write_bytes(report)
        with closing(sqlite3.connect(data / "jobs.sqlite3")) as connection, connection:
            connection.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, question TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                pages INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0,
                done_chunks INTEGER DEFAULT 0, error TEXT DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO jobs (id,filename,question,status,created_at,updated_at) "
                "VALUES (?, 'old.pdf', 'Existing request', 'done', '2025-01-01', '2025-01-01')",
                (identifier,),
            )
        prompt = directory / "prompt.txt"
        prompt.write_text("Offline smoke test; no analysis is submitted.")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = dict(
            os.environ,
            DATA_DIR=str(data),
            SYSTEM_PROMPT_FILE=str(prompt),
            APP_PORT=str(port),
            LLAMA_BASE_URL="http://127.0.0.1:1",
            LLAMA_API_KEY="",
            OMP_THREAD_LIMIT="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        with (directory / "server.log").open("w+") as log:
            server = subprocess.Popen(
                [sys.executable, "-u", str(root / "app.py")],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    if server.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(f"Server did not start: {log.read()}")
                    try:
                        with urllib.request.urlopen(
                            base + "/api/health", timeout=1
                        ) as response:
                            health = json.load(response)
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                assert health == {"status": "ok", "version": __version__}, health
                for suffix, expected in (
                    (f"/api/jobs/{identifier}/report", report),
                    (f"/api/jobs/{identifier}/report/view?lang=fr", b'lang="fr"'),
                    (f"/api/jobs/{identifier}/report/view?lang=en", b'lang="en"'),
                ):
                    with urllib.request.urlopen(base + suffix, timeout=2) as response:
                        content = response.read()
                    assert expected in content, suffix
                with urllib.request.urlopen(base + "/api/jobs", timeout=2) as response:
                    assert json.load(response)["jobs"][0]["status"] == "done"
                with urllib.request.urlopen(
                    base + f"/api/jobs/{identifier}/live", timeout=2
                ) as response:
                    assert json.load(response)["active"] is False
                assert previous.read_bytes() == b"Previous report: keep this file.\n"

                # Real extraction and Tesseract from the runtime image, without a model.
                pdf = directory / "sample.pdf"
                sentence = (
                    "Scientific measurements and observations provide reliable results."
                )
                with pymupdf.open() as source:
                    source.new_page().insert_text((50, 80), sentence, fontsize=14)
                    image = source[0].get_pixmap(dpi=150, alpha=False).tobytes("png")
                    with pymupdf.open() as document:
                        document.insert_pdf(source)
                        scan = document.new_page()
                        scan.insert_image(scan.rect, stream=image)
                        document.save(pdf)
                pages, count, missing, ocr = extract_pdf(
                    pdf,
                    dict(
                        enabled=True,
                        languages="eng+fra",
                        dpi=150,
                        max_pixels=5_000_000,
                        timeout=30,
                        max_pages=120,
                    ),
                )
                assert count == 2 and missing == [] and ocr == [2]
                assert all("scientific" in page["text"].lower() for page in pages)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                    raise
            log.seek(0)
            logs = log.read()
            assert server.returncode == 0, logs
            assert "worker_stopped" in logs, logs
        # Exercise the coherent SQLite backup used by the deployment guide.
        with (
            closing(sqlite3.connect(data / "jobs.sqlite3")) as source,
            closing(sqlite3.connect(directory / "backup.sqlite3")) as target,
        ):
            source.backup(target)
            assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                target.execute("SELECT question FROM jobs").fetchone()[0]
                == "Existing request"
            )
            assert target.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 0
            assert target.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
                0
            ] == len(database.MIGRATIONS)
        print(
            f"Runtime smoke passed: version {__version__}, legacy migration, EN/FR report, live, real OCR, SIGTERM and SQLite backup; no generation."
        )


if __name__ == "__main__":
    main()
