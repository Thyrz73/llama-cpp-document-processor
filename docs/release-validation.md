# Release 2.0.0 preparation: measured validation

Evidence collected on 8 November 2026. Preparation started at `5c5320e36446553eeef41849eec2e8874e243e08`, matching GitHub. The health response now additionally exposes the existing application version `2.0.0`; SQL, extraction, generation, snapshots and retention behavior are unchanged by release preparation.

## Functional and static checks

- Python **3.12.16**: **370 tests passed** under coverage, including existing-database migrations, recovery, verified artifact reuse/refusal, cancellation, live output and French/English reports. Combined statement/branch coverage is **96.29%**, above the unchanged 96% floor.
- Ruff **0.17.3**: lint and formatting passed. Mypy **2.4.1**: 36 source files passed; the five selected contract/configuration modules also passed strict mode.
- Node **22.23.4**: **73 tests in 7 suites passed**. Prettier **3.6.2** passed with the integrity-locked npm install. JavaScript syntax, Python compilation and the offline `scripts/benchmark.py` plan passed.
- `tests/smoke_container.py` passed on the Python 3.12 host: real server startup, migration of an existing unversioned SQLite row, unchanged old report bytes, French/English reader, inactive live route, retained `previous_reports`, real mixed-PDF/Tesseract OCR, SIGTERM and coherent SQLite backup/integrity. Zero analysis attempts were recorded.
- Existing regression tests cover snapshot/manifest retention, source/model/template identity changes, reusable completed work after cancellation and restart, and refusal to treat incomplete generation as completed output. No real llama.cpp generation was requested.

The sandbox blocks local sockets. Integration tests and the smoke used the same source/tools with loopback enabled outside that restriction. Node's restricted run reported only the test-file wrapper; the unrestricted run above confirmed all 73 individual cases. These environment restrictions were not treated as passing coverage of the affected checks.

## GitHub CI and Docker distinction

[GitHub Actions run 42431324774](https://github.com/Thyrz73/llama-cpp-document-processor/actions/runs/42431324774) succeeded for **5c5320e**, including Python/JS checks, Compose validation and Docker build. Its recorded run span was 00:46:56–00:48:20 UTC on 8 November 2026. This is evidence for the starting revision, not for later local commits.

Docker is unavailable on the local WSL2 execution environment. Therefore **the new constrained image smoke has not been executed in Docker here**. CI now runs it after building the image, with no external network, 512 Mio RAM, no extra swap, 0.75 CPU, 64 PIDs, an init process and `OMP_THREAD_LIMIT=1`. Require this check on the final PR head after an authorized push. No remote workflow was triggered by this preparation.

The single CI job remains: no speedup from parallelizing setup has been demonstrated. Dockerfiles, runtime dependency pins and the existing Compose limits are preserved.

## Comparable source-baseline measurements

No historical timing capture from step 00 was found. The reference is therefore **reconstructed**, not presented as numbers recorded during step 00: `f55eb017ed947b7d07e436e4c00c3c26d9f65bb6`, the immediate parent of step-01 commit `8a8b938`. Its source was exported without checkout, merge or rebase.

Both variants ran on the same unrestricted WSL2 host with Python 3.12.16, SQLite 3.53.1, PyMuPDF 1.28.2, pypdf 6.21.4, Flask 3.1.3, Waitress 3.0.2 and Tesseract 5.5.0 (`eng+fra`, one OCR thread). Using the current locked dependencies for both isolates source changes; it does not recreate unknown historical machine/package conditions. Three fresh component processes and three empty-queue Waitress/worker processes per variant were measured sequentially. See [raw samples](benchmarks/release-2.0.0.json).

| Metric | Reconstructed step 00 | Release preparation |
| --- | ---: | ---: |
| SQL list/order, 100 of 1,000 jobs | 865.019 µs | 178.993 µs |
| SQL global active-job lookup | 46.898 µs | 2.700 µs |
| SQL queued-job selection, empty queue | 39.498 µs | 0.900 µs |
| SQL progress updates, 120 page callbacks | 120 | 3 |
| Preview file writes, same page callbacks | 120 | 2 |
| Preview file writes, 102 live callbacks | 102 | 6 |
| Offline tokenizer endpoint calls, 120 pages | 239 | 139 |
| Chunks at the same simulated 3,000-token budget | 10 | 10 |
| Real text extraction, 120 short pages | 53.55 ms | 53.62 ms |
| Real OCR extraction, one scan page | 288.12 ms | 291.19 ms |
| Idle server RSS | 69.61 Mio | 68.25 Mio |
| Idle server PSS | 63.63 Mio | 62.32 Mio |

SQL values are medians of three per-process medians, each over 200 warmed reads. These are SQL microbenchmarks on disposable 1,000-job histories, not HTTP latency. History rows are terminal, so active/claim queries return no active row. Per-process p95 values are retained in the JSON.

Preview counts wrap real atomic writes and real SQLite updates in temporary data. Progress spans 1.19 simulated seconds; live spans 80 simulated seconds with 0.8-second callbacks and a terminal update. They measure deterministic write counts, not real generation speed. Tokenization uses the real client/cache and chunker against a deterministic local endpoint simulator; no server vocabulary/network latency is claimed. Chunk budgets and lossless source passage content are checked.

Extraction and idle-memory figures are medians of three observations. The text fixture has 120 short pages, not a maximum-size upload. Idle RSS/PSS belongs to the actual server with an empty worker queue after health/list requests, sampled five times per process; it is not peak OCR memory. The small extraction timing differences do not establish a gain or regression. No 512-Mio/0.75-CPU capacity conclusion follows from these unrestricted measurements.

## Reproduce

From a clean repository with Python 3.12 locked dependencies, Tesseract `eng`/`fra`, and permission to bind loopback sockets:

```bash
release_reference=$(mktemp -d /tmp/processor-step00-XXXXXX)
git archive f55eb017ed947b7d07e436e4c00c3c26d9f65bb6 | tar -x -C "$release_reference"
.venv/bin/python tests/benchmark_release.py \
  --repository "$release_reference" --label f55eb01 > /tmp/processor-baseline.json
.venv/bin/python tests/benchmark_release.py \
  --repository "$PWD" --label release > /tmp/processor-release.json
.venv/bin/python tests/smoke_container.py
```

Use the **same Python executable and dependency environment** for both benchmark commands, set no real backend credentials, and avoid concurrent tests/workloads during timing. Each benchmark uses disposable data, forces a non-serving loopback backend and forbids Requests transport in component measurements. No user database/PDF is opened. The reference directory and output files can be retained for review.

## Remaining release gates and compatibility limits

The final local commits still require an authorized push and a green final-head CI run, including the newly added Docker smoke. Rehearse the complete image/state rollback on the target host before deployment; follow [releasing.md](releasing.md).

Extraction-process isolation was evaluated and deferred: exploratory `spawn` measurements added 31–46 Mio PSS at peak without an extraction speedup. Tesseract is already a subprocess, but cancellation waits for the current OCR call or its timeout (180 seconds by default); native PDF reading/rendering has no hard process deadline. Pixel/upload bounds do not bound every native allocation. Windows-native process supervision and constrained peak capacity were not validated.

No snapshot, old report, manifest or `previous_reports/` is removed. Existing identity, text-transport, artifact and pipeline compatibility checks still govern reuse. No automatic token-limit continuation is introduced; [continuation prerequisites](releasing.md#before-continuation-after-token-limits) remain a separate feature.
