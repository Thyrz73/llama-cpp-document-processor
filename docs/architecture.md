# Architecture

How the code is organized, how a job moves through it and where to make common changes. For installation and usage, see the [README](../README.md).

## Overview

```text
app.py ──▶ lifecycle.run_server() ──▶ Waitress + Worker + SQLite shutdown
   └─────▶ application.create_app() / compatibility wiring
                        │
Browser ──HTTP──▶ web.system / web.jobs / web.reports
                        │ app.extensions["document_processor"]
                        ▼
                     Services ──▶ JobStore ──▶ SQLite + job files
                        ├───────▶ CapabilityCache + DeploymentModelClient
                        └───────▶ Worker ──▶ pipeline.JobRun ──▶ llama.cpp
```

There is one process, one worker thread and a few web threads. SQLite holds the queue and every job record; each job also has a private directory for its PDF, intermediate results and report.

## Modules

| File | Responsibility |
| --- | --- |
| `app.py` | Default application and Waitress entry point; installs the legacy compatibility exports. |
| `document_processor/application.py` | `create_app(settings, store, client_factory, *, worker)`: Flask setup, assets, blueprints and private failure logging. |
| `document_processor/services.py` | Application-owned settings, store, worker, client construction, snapshots and pipeline wiring. |
| `document_processor/backend.py` | `DeploymentModelClient` and one backend-keyed TTL cache/inspection lock per application. |
| `document_processor/web/system.py` | Page, health, profiles, configuration preview and read-only diagnostics. |
| `document_processor/web/jobs.py` | Uploads, job status/metrics/live, retry, cancellation and history deletion. |
| `document_processor/web/reports.py` | Validated report paths, streaming downloads and the bounded Markdown viewer. |
| `document_processor/web/hooks.py` | Cross-origin write rejection, no-store/nosniff headers and stable public errors. |
| `document_processor/lifecycle.py` | Startup validation, database initialization, Waitress, SIGTERM and ordered worker/SQLite shutdown. |
| `document_processor/compat.py` | Explicit adapters for old `import app` tests; never used by independently created apps. |
| `document_processor/settings.py` | Immutable deployment scalars and paths, loaded explicitly by `Settings.from_env()`; fresh job defaults. |
| `document_processor/environment.py` | Parses environment variables; errors name the variable. |
| `document_processor/contracts.py` | Static job/snapshot, extraction, note and completion dictionary contracts; see [type contracts](type-contracts.md). |
| `document_processor/config.py` | Turns job options into an immutable, validated configuration snapshot. |
| `document_processor/profiles.py` | The `low`…`ultra` token budget profiles. |
| `document_processor/prompts.py` | Task prompt templates, report header and language helpers. |
| `document_processor/errors.py` | Public error types: each carries a stable code translated by the interface. |
| `document_processor/observability.py` | Structured log events and the "job being processed" log context. |
| `document_processor/files.py` | Atomic file writes and file hashing. |
| `document_processor/database.py` | SQLite connections and numbered schema migrations. |
| `document_processor/jobs.py` | `JobStore`: the only code that reads or writes job records and attempt metrics. It also checks job directory paths and deletes directories safely. |
| `document_processor/artifacts.py` | Reuses intermediate results only when their inputs and content still match. |
| `document_processor/extraction.py` | PDF text with PyMuPDF, pypdf fallback, Tesseract OCR, section splitting. |
| `document_processor/model_client.py` | llama.cpp HTTP client, independent `StreamParser`/typed `StreamState`, tokenization, deadlines and cancellation. |
| `document_processor/reliability.py` | Mechanical checks of citations, numbers and certainty wording. |
| `document_processor/reports.py` | Report download names and safe Markdown rendering. |
| `document_processor/pipeline.py` | `JobRun`: the analysis steps of one job. |
| `document_processor/previews.py` | Thread-safe bounded live-preview cache; JSON checkpoints remain the restart/process fallback. |
| `document_processor/worker.py` | `Worker`: claims queued jobs, runs them and records how they ended. |
| `static/js/` | Browser interface: ES modules started by `main.js` (see [Browser interface](#browser-interface)). |
| `static/`, `templates/` | Page template, styles, report viewer script and the English/French `translations.json`. |
| `scripts/` | `check_jobs.py` (read-only data check) and `benchmark.py`. |
| `tests/` | `unittest` suites and the Node.js interface tests (`test_polling.js`). |

## Deployment settings

Importing `document_processor.settings` performs no environment reads, file access, logging setup or service initialization. `app.py` calls `Settings.from_env()` once when it loads; Compose supplies `.env` values, and Python never reads `.env` itself. The frozen, slotted dataclass contains only immutable scalars and `Path` values. Credentials, backend URLs and local paths are excluded from its representation. `job_defaults()` returns fresh dictionaries without credentials, backend URLs or paths; public snapshots still pass through `config.public_config()`.

Validation keeps its existing two stages: `from_env()` applies `environment.py` parsing and individual environment bounds in their original order; `config.resolve()` checks job limits and cross-budget consistency when the server starts and when jobs/configurations are resolved. Relative paths, unstripped keys, request/step timeout fallback and the 1,000–50,000 live-preview clamp retain their previous semantics. The Flask request limit remains the upload MiB limit plus 65,536 bytes for multipart data.

`app.SETTINGS` is the initial process snapshot of the compatibility entry point. `compat.py` temporarily exports the original uppercase globals and no-argument hooks through `app.py`; `app.deployment_settings()` explicitly reads those names without rereading the environment. Existing monkeypatches apply only to this default app. `app.ModelClient` is a compatibility constructor for `LegacyDeploymentClient`; production factory instances construct the distinctly named `DeploymentModelClient`. Code importing the reusable backend adapter still uses `model_client.ModelClient`. The step-11 `settings=` hooks remain available on the compatibility functions. New fixtures should inject settings, stores and clients through `create_app()` instead of adding more global patches.

Only the system prompt's **path** belongs to `Settings`. `read_system_prompt()` reads its current UTF-8 content for a new snapshot (or a client without a saved prompt); editing the file affects new jobs, while existing jobs and retries keep their saved text. Missing, unreadable and empty prompts retain their existing errors. Neither parsing settings nor building default dictionaries reads or validates prompt contents.

## Factory, dependencies and lifecycle

`create_app(settings, store=None, client_factory=None, *, worker=None)` constructs an isolated application with the same asset roots, upload limit, routes and protections. `settings` is explicit and immutable. A missing store is constructed from `settings.data_dir`; a missing worker is constructed for that store. Construction never initializes SQLite, creates an HTTP client, starts a thread, reads the private prompt or contacts llama.cpp. Importing the factory never imports `app.py` or reads deployment environment values. Importing `app` creates its compatibility app but starts no worker/network activity.

The single service object is available as `application.extensions["document_processor"]`; request handlers retrieve it from the current Flask application. `Services` owns the store, worker and backend cache, and provides `new_snapshot()` and `analyze_job()` for work outside a request context. `client_factory(config)` is optional: it replaces model construction for pipeline work and read-only diagnostics. Diagnostics pass an empty `system_prompt` and never read the private file. `worker` can be a test double implementing the same wake/cancel/lifecycle methods. Each factory call creates its own store/worker by default and always owns a distinct capability cache/lock. Callers may explicitly inject shared collaborators; independently created apps do not observe compatibility-global patches.

Tests initialize their injected store explicitly, call `application.test_client()`, then close the store in cleanup. The migrated history/report suites and `test_application.py` show this pattern. The worker is not started as part of a request or request-context setup/teardown. There is deliberately no per-request SQLite close: connections remain owned/reused by their thread, and preview/public-configuration caches retain their established lifetimes.

`python app.py` alone calls `lifecycle.run_server()`: validate job defaults, initialize/recover the database, install SIGTERM, start the worker and serve Waitress on `0.0.0.0` with the configured port and four threads. Shutdown requests cooperative worker stop before closing/checkpointing the main thread's SQLite connection; the worker still closes its own connection. Merely creating/importing an app does none of these startup steps. Joint-budget and schema errors retain their messages and exit behavior.

Blueprints namespace internal Flask endpoints (`system.index`, `jobs.create_job`, `reports.report`, etc.); HTTP paths and methods are unchanged. Report templates use those names and generate the same URLs. Logs remove the namespace when reporting endpoint names, preserving the existing public log fields. CSP, download streaming/headers, report-view i18n, cookie behavior, path checks and public error handlers remain application-wide or with their owning route group.

## Life of a job

1. **Upload.** `POST /api/jobs` first calls `config.resolve()`, which combines administrator defaults, the chosen profile and overrides into a configuration snapshot. The snapshot also stores the system prompt and task templates. The route then checks the request and the PDF, saves the file and adds its hash to the snapshot. `JobStore.create_job()` saves the job as `queued`, and the route wakes the worker.
2. **Claim.** `Worker.run_forever()` claims the oldest queued job (`JobStore.claim_next_job()`) and calls `Services.analyze_job()` (also exposed as `app.analyze_job` for compatibility), which builds a `pipeline.Runtime` with the store, the model client factory and the worker's cancellation check.
3. **Analysis.** `JobRun.execute()` prepares the backend, extracts the text, splits it into sections, writes notes per section, merges them until they fit the final prompt, writes the final report and saves it. The extracted text, sections, notes, merges and final text are saved as artifacts, so a retry reuses finished work.
4. **Progress.** Each phase updates the job row. When live output is enabled, previews update memory and periodically checkpoint `live_preview.json`. The browser polls `GET /api/jobs` every 6 seconds while jobs are active. An open live panel for a queued/running job schedules its next `GET /api/jobs/<id>/live` 1.7 seconds after the previous request settles. Model-pass `active=false` does not stop polling a running job. Terminal jobs get one final preview; opening historical output and changing channels remain one-off loads. Closing the panel, hiding the tab or stopping/resetting the UI cancels its timeout; late responses cannot restart an old UI.
5. **End.** The job becomes `done`, `failed` (with a translated error code) or `cancelled`. Failed and cancelled jobs can be retried with the same snapshot, up to `MAX_JOB_ATTEMPTS` attempts in total.

`POST /api/jobs/<id>/cancel` records the request in SQLite, then signals the running job in memory. The job checks for it at each phase change, tokenizer call and streamed token, and every 0.2 seconds while it waits for the model. After a restart, `JobStore.initialize()` requeues interrupted jobs and completes pending cancellations.

`ModelError` and `Cancelled` both inherit from `ProcessingError` (`RuntimeError`), but cancellation is not a backend failure. Optional backend inspection fallbacks catch `ModelError` and let `Cancelled` propagate. The stream reader passes cancellation to its consumer, and the pipeline and worker record it as `cancelled`, including during report saving. `ConfigurationError` remains a `ValueError` with its existing public code and HTTP 400 response.

`ModelClient.stream_lines()` owns HTTP, byte-to-line reassembly, the global single-reader gate, deadlines and cancellation/closure. `StreamParser.feed()` consumes those complete SSE lines without network access: UTF-8/JSON decoding, deltas, fragmented `<think>` markers, reasoning channels and engine measurements. A `finish_reason` does not end reading early because usage-only events can follow it. `[DONE]` ends ingestion; `finish()` flushes pending text and rejects truncation, unsupported endings, incomplete streams and empty answers before marking the typed `StreamState` complete. `generate()` owns request options, preview scheduling and failure categories. State snapshots assemble text only when requested, keep a bounded reasoning tail and share the generation's metrics dictionary; character counters retain total lengths. No partial response becomes a `CompletionResult` or a reusable pipeline artifact.

Backend diagnostics use the application-owned `CapabilityCache` in `backend.py`: 30 seconds for success with a known context, 5 seconds otherwise. `inspect()` allows TTL observations for display; `inspect_identity()` always reads `/v1/models` and `/props` afresh at the beginning of each attempt, before artifact compatibility checks. Its result is reused within the attempt and supplies the UI cache. A lock serializes inspections; jobs can cancel while waiting for it. Backend failures invalidate observations; callers receive copies, and cache reads never renew expiration. Profiles, preview and creation only read the cache and retain `engine_context_unknown` when no valid context is known. Tests in `test_capabilities.py` and `test_backend_identity.py` simulate time and HTTP responses without a real backend.

Existing artifacts require known model, template and engine-sampling hashes in both their manifest and the fresh identity. A mismatch or unverifiable identity raises the existing `identity_changed` error before any artifact read, without rewriting the manifest or snapshot. Limited-property backends can run a new analysis, but cannot reuse artifacts with unknown identity fields. The version field remains optional and is compared when recorded previously.

## Browser interface

`templates/index.html` loads `static/js/main.js` as an ES module, which calls `start()` in `app.js`. There is no build step: browsers load the modules directly.

| Module | Responsibility |
| --- | --- |
| `app.js` | `start()`: connects the page controls to the other modules and loads the list. |
| `state.js` | All mutable interface state, in a single `state` object. |
| `jobs.js`, `card.js` | The analyses list: polling, rendering, the retry, cancel and delete actions, and the notice reporting their outcome. |
| `live.js`, `metrics.js` | The live output and metrics panels of a job. |
| `history.js` | History selection and the deletion dialog. |
| `settings.js`, `upload.js` | The analysis settings with their server preview, and the upload form. |
| `i18n.js`, `theme.js`, `api.js`, `dom.js` | Translations, color theme, JSON requests and DOM helpers. |

Modules keep no state of their own and do nothing when imported; only `main.js` starts the interface. Their imports form no cycle (`tests/test_architecture.py`), and `tests/test_polling.js` runs the real modules against a simulated browser.

## Design rules

- **One owner per concern.** Job queries live in `jobs.py` and schema migrations in `database.py`. PDF handling lives in `extraction.py`, and llama.cpp access in `model_client.py`.
- **One database block at a time.** A `with store.connect()` block runs on its thread's own connection and cannot contain another one: call other `JobStore` methods after it. `JobStore` methods each use a single block.
- **No dependency on the web app.** Package modules never import `app.py`, and their imports form no cycle. `tests/test_architecture.py` enforces both.
- **Immutable jobs.** A job keeps its configuration snapshot forever; retries reuse it. To use new settings, create a new analysis.
- **Public errors are coded.** Raise `ProcessingError(code, message)`, or a subclass, and translate the code in both languages of `static/translations.json`. `tests/test_errors.py` keeps codes and translations in sync.
- **Private data stays private.** Logs and metrics never contain document text, prompts or model output. The API returns model output only through the report routes and the optional live preview, and never returns the system prompt or the saved templates.

## Resource use

The application targets small machines. The defaults are 512 MB of memory and 0.75 CPU for the container.

- One worker processes one job at a time, and the model client never runs two generations at once.
- Each attempt consumes the just-generated reliability audit once when saving its checks, keyed by exact text, physical page scope, reliability version and evidence batch. Reused artifacts are audited afresh. NFKC source entities and quotations are prepared once per unchanged evidence batch in memory; `source_evidence.json`, quality-check ordering, metrics and artifact versions are unchanged. Extraction fingerprints are still computed separately for evidence and chunk dependencies because the extracted data is mutable.
- Stream character accounting is incremental in `ThinkingSplitter.append()`, where text is assigned to its actual channel. Content counts exclude reasoning/tags and include pending partial tags only when flushed; reasoning counts retain total emitted length even when previews are truncated. Live/final metrics and the 4,000,000-character content limit use these counts without rescanning prior fragments. The limit is checked again after the final pending-text flush.
- Each thread keeps one SQLite connection open, so an update no longer pays for opening a connection, and reads write nothing. WAL keeps `synchronous=NORMAL`: committed WAL is recoverable after process interruption, but a power failure can lose recent commits. Cancellation checks are answered in memory, and SQLite is reread at most once per second.
- History lists cache at most 256 immutable public configurations per `JobStore`, keyed by job ID and the stored fingerprint. A lock serializes fills and invalidation; callers receive copies of public fields only. Warm lists select fingerprints instead of private snapshot JSON, and misses are loaded in one batch. Job state and the global active-job flag are always read afresh. Deletion invalidates after commit, saving a legacy configuration invalidates that job, startup/migration recovery clears the cache, and changed fingerprints bypass old entries automatically. Detailed configuration reads still verify snapshot integrity.
- Extraction keeps every per-page cancellation callback, but persists ordinary progress to SQLite, with its phase log, at most once per second using a monotonic clock. The first and last page, every extraction/OCR transition and all later pipeline phases persist immediately. Previews update memory at the same time; disk records extraction/OCR transitions and the last page. Percentages retain the page-based calculation and never decrease within extraction; worker failure, cancellation and final status updates bypass this throttle.
- `SIGTERM` requests cooperative worker shutdown and waits at most 5 seconds. The internal `WorkerStopping` signal marks attempts and steps `interrupted`, leaving jobs recoverable at startup instead of cancelling them. A worker closes its own connection on exit, without a redundant checkpoint. The main thread then attempts one `TRUNCATE` checkpoint with a 1-second lock timeout. Busy/incomplete results are logged with frame counts; SQLite exceptions are logged by type without private messages. HTTP threads retain their own connections; other writers or readers can still prevent truncation. A non-cooperative operation can exceed the worker wait: a warning is emitted and the daemon does not prevent process exit. Preserve the WAL and use a coherent backup; a checkpoint is not a backup barrier against future writes.
- OCR renders one page at a time within a pixel budget. Each result is written to disk as soon as it is ready, so a crash or a retry only repeats unfinished work.
- Each `JobStore` owns a process-local preview cache protected by a lock: at most 16 snapshots, with both text channels limited to `preview_chars` and the existing phase/count/metric fields. Snapshots and reads are independent copies, so reasoning redaction cannot alter another response. Entries expire lazily 5 minutes after publication (polling does not extend their life); attempts release them on exit, retries discard them, and deletion removes them after the database commit. Startup recovery clears the cache.
- Generation updates memory at the existing 0.8-second cadence. Disk checkpoints are atomic at generation start/end, phase/pass boundaries, reused outputs, and every 20 seconds when streamed text or reasoning is present. Failure, cancellation and cooperative shutdown flush the latest published partial output as inactive before propagating the original error. A hard process failure can lose up to one checkpoint interval of text that was only in memory; durable pipeline artifacts and their reuse rules are unchanged.
- The live route prefers memory and falls back to JSON without caching disk reads. The internal `_attempt_id` checkpoint field is compared with SQLite's latest attempt and stripped from the response; queued retries and claimed jobs awaiting a new attempt cannot activate old output. Legacy JSON remains readable for historical jobs. With Flask and the worker in one process, the existing 1.7-second polling sees current memory. Separate processes see disk checkpoints (up to 20 seconds apart); sharing the faster cadence across processes would require shared memory or a publication service. The browser stops polling when no job is active or when the tab is hidden.

## Common changes

| Change | Where | Checked by |
| --- | --- | --- |
| Add a setting | Add a field and parse it in `Settings.from_env()` with `environment.*`; update the temporary exports/adapter in `compat.py`, `compose.yaml`, `.env.example` and the README table | Extend `tests/test_settings.py` and the `names` table of `tests/test_environment.py`, which compares code and Compose defaults |
| Change the database schema | Append a function to `database.MIGRATIONS`; never edit a released one | `tests/test_database.py` |
| Add an error | `raise ProcessingError("my_code", "Message.")`, then add `error.my_code` to both languages | `tests/test_errors.py` |
| Change a task prompt | `prompts.TEMPLATES`, and increase `PROMPT_VERSION` | Existing jobs keep their saved prompts |
| Change a profile | `profiles.PROFILES`, increase `PROFILE_VERSION`, then update the README table | `tests/test_v2.py` |
| Add an API route | A route in the relevant `web` blueprint that calls application services/`JobStore` methods, then document it in [api.md](api.md) | Route tests in `tests/` |
| Change the interface | The module of the [Browser interface](#browser-interface) concerned; add new text to both languages of `static/translations.json` | `tests/test_polling.js` |

## Tests

Run the commands from the README's Development section. Python tests use temporary data directories and simulated model responses, so they need no llama.cpp server. Notable suites:

- `test_v2.py`, `test_reliability.py`, `test_encoding.py`: full pipeline runs, artifact reuse, retries and cancellation.
- `test_progress.py`: job states and progress, restart recovery and the worker loop.
- `test_public_configs.py`: bounded public-only configuration caching, fingerprint changes, invalidation, concurrent list reads and active jobs outside the visible history.
- `test_database.py`, `test_worker_shutdown.py`: migrations, connection reuse/ownership, rollback, concurrent checkpoints, bounded shutdown, SQLite backup and WAL recovery after abrupt process exit.
- `test_application.py`: per-app store/worker/cache isolation, route/method inventory, injected clients and workers, JSON/security/upload/live/report contracts, side-effect-free import and explicit lifecycle order.
- `test_contracts.py`: JSON/fingerprint round-trips, historical snapshots, public projections, extraction/note provenance and incremental completion measurements.
- `test_stream_parser.py`, `test_stream_metrics.py`: deterministic parser sequences, stable errors/metrics/budgets, fragmented thinking, HTTP cleanup, immediate cancellation, single-reader ownership and rejection of partial artifacts. `test_encoding.py` also fragments actual response bytes, including UTF-8 characters, before HTTP line reassembly.
- `test_settings.py`: deployment defaults/overrides, validation stages, immutable values, isolated defaults, import behavior, compatibility globals, explicit injection and editable prompts.
- `test_architecture.py`, `test_errors.py`, `test_environment.py`, `test_dependencies.py`: guards against import cycles, untranslated errors, configuration drift and diverging dependency locks. `test_environment.py` checks real HTTP shutdown after a completed job, plus the application SIGTERM path during a simulated worker transaction. Single-file checks apply to these quiescent test scenarios, not every production stop.
- `test_polling.js`: the interface modules against a simulated browser: polling, rendering, live output, settings and history, run with Node.js.
