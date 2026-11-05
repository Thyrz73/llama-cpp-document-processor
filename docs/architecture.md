# Architecture

How the code is organized, how a job moves through it and where to make common changes. For installation and usage, see the [README](../README.md).

## Overview

```text
Browser ──HTTP──▶ app.py (Flask routes) ──▶ jobs.JobStore ──▶ data/jobs.sqlite3 + data/jobs/<id>/
                       │ wake / cancel
                       ▼
                  worker.Worker (one background thread)
                       │ runs one job at a time
                       ▼
                  pipeline.JobRun ──▶ model_client ──HTTP──▶ llama.cpp
                       ├── extraction   PDF text, OCR, sections
                       ├── artifacts    verified reuse of finished steps
                       └── reliability  mechanical citation and number checks
```

There is one process, one worker thread and a few web threads. SQLite holds the queue and every job record; each job also has a private directory for its PDF, intermediate results and report.

## Modules

| File | Responsibility |
| --- | --- |
| `app.py` | Deployment settings, Flask routes and startup. Only HTTP handling and wiring. |
| `document_processor/environment.py` | Reads environment variables; errors name the variable. |
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
| `document_processor/model_client.py` | llama.cpp HTTP client: tokenization, streaming, deadlines, cancellation. |
| `document_processor/reliability.py` | Mechanical checks of citations, numbers and certainty wording. |
| `document_processor/reports.py` | Report download names and safe Markdown rendering. |
| `document_processor/pipeline.py` | `JobRun`: the analysis steps of one job. |
| `document_processor/worker.py` | `Worker`: claims queued jobs, runs them and records how they ended. |
| `static/js/` | Browser interface: ES modules started by `main.js` (see [Browser interface](#browser-interface)). |
| `static/`, `templates/` | Page template, styles, report viewer script and the English/French `translations.json`. |
| `scripts/` | `check_jobs.py` (read-only data check) and `benchmark.py`. |
| `tests/` | `unittest` suites and the Node.js interface tests (`test_polling.js`). |

## Life of a job

1. **Upload.** `POST /api/jobs` first calls `config.resolve()`, which combines administrator defaults, the chosen profile and overrides into a configuration snapshot. The snapshot also stores the system prompt and task templates. The route then checks the request and the PDF, saves the file and adds its hash to the snapshot. `JobStore.create_job()` saves the job as `queued`, and the route wakes the worker.
2. **Claim.** `Worker.run_forever()` claims the oldest queued job (`JobStore.claim_next_job()`) and calls `app.analyze_job()`, which builds a `pipeline.Runtime` with the store, the model client factory and the worker's cancellation check.
3. **Analysis.** `JobRun.execute()` prepares the backend, extracts the text, splits it into sections, writes notes per section, merges them until they fit the final prompt, writes the final report and saves it. The extracted text, sections, notes, merges and final text are saved as artifacts, so a retry reuses finished work.
4. **Progress.** Each phase updates the job row and, when live output is enabled, `live_preview.json`. The browser polls `GET /api/jobs` every 6 seconds while jobs are active, and `GET /api/jobs/<id>/live` every 1.7 seconds while a live panel is open.
5. **End.** The job becomes `done`, `failed` (with a translated error code) or `cancelled`. Failed and cancelled jobs can be retried with the same snapshot, up to `MAX_JOB_ATTEMPTS` attempts in total.

`POST /api/jobs/<id>/cancel` records the request in SQLite, then signals the running job in memory. The job checks for it at each phase change, tokenizer call and streamed token, and every 0.2 seconds while it waits for the model. After a restart, `JobStore.initialize()` requeues interrupted jobs and completes pending cancellations.

`ModelError` and `Cancelled` both inherit from `ProcessingError` (`RuntimeError`), but cancellation is not a backend failure. Optional backend inspection fallbacks catch `ModelError` and let `Cancelled` propagate. The stream reader passes cancellation to its consumer, and the pipeline and worker record it as `cancelled`, including during report saving. `ConfigurationError` remains a `ValueError` with its existing public code and HTTP 400 response.

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
- Each thread keeps one SQLite connection open, so an update no longer pays for opening a connection, and reads write nothing. In WAL mode, commits do not wait for the disk (`synchronous=NORMAL`): a power failure can undo the latest updates but never corrupts the database. A normal stop (`SIGTERM`, sent by `docker compose stop`) folds the log into `jobs.sqlite3`; after a crash, the `-wal` file still holds the latest changes. Cancellation checks are answered in memory, and SQLite is reread at most once per second.
- OCR renders one page at a time within a pixel budget. Each result is written to disk as soon as it is ready, so a crash or a retry only repeats unfinished work.
- Live previews are bounded. The browser stops polling when no job is active or when the tab is hidden.

## Common changes

| Change | Where | Checked by |
| --- | --- | --- |
| Add a setting | Read it in `app.py` with `environment.*`, then add it to `compose.yaml`, `.env.example` and the README table | Add it to the `names` table of `tests/test_environment.py`, which compares code and Compose defaults |
| Change the database schema | Append a function to `database.MIGRATIONS`; never edit a released one | `tests/test_database.py` |
| Add an error | `raise ProcessingError("my_code", "Message.")`, then add `error.my_code` to both languages | `tests/test_errors.py` |
| Change a task prompt | `prompts.TEMPLATES`, and increase `PROMPT_VERSION` | Existing jobs keep their saved prompts |
| Change a profile | `profiles.PROFILES`, increase `PROFILE_VERSION`, then update the README table | `tests/test_v2.py` |
| Add an API route | A route in `app.py` that calls `JobStore` methods, then document it in [api.md](api.md) | Route tests in `tests/` |
| Change the interface | The module of the [Browser interface](#browser-interface) concerned; add new text to both languages of `static/translations.json` | `tests/test_polling.js` |

## Tests

Run the commands from the README's Development section. Python tests use temporary data directories and simulated model responses, so they need no llama.cpp server. Notable suites:

- `test_v2.py`, `test_reliability.py`, `test_encoding.py`: full pipeline runs, artifact reuse, retries and cancellation.
- `test_progress.py`: job states and progress, restart recovery and the worker loop.
- `test_database.py`: migrations of databases from every past release, and the per-thread connections (reuse, rollback, nesting, concurrent threads, closing the log).
- `test_architecture.py`, `test_errors.py`, `test_environment.py`, `test_dependencies.py`: guards against import cycles, untranslated errors, configuration drift and diverging dependency locks. `test_environment.py` also starts the real `app.py` and checks that `SIGTERM` stops it cleanly with every change in `jobs.sqlite3`.
- `test_polling.js`: the interface modules against a simulated browser: polling, rendering, live output, settings and history, run with Node.js.
