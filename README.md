# Document Processor for llama.cpp

A self-hosted PDF analysis app for an **existing llama.cpp server**. Upload a PDF, ask a question and get a Markdown report whose claims cite PDF pages as `[Page N]`. The app does not install a model or reconfigure llama.cpp.

- Selective OCR (English and French) for scanned pages, with PyMuPDF and pypdf for text PDFs.
- Performance profiles, report language and detail level, all saved with each analysis.
- Live output, cancellation and retries that reuse finished steps.
- Mechanical checks of page citations and figures, reported as warnings.
- English/French interface, history with deletion, light and dark themes.
- Built for small machines: one worker, and the processor itself needs no GPU, embeddings or extra services; its container is limited to 512 MB and 0.75 CPU by default.

## Quick start

You need Git, Docker Compose and a reachable llama.cpp server with a loaded chat model. The app uses `/v1/models`, `/tokenize` and streaming `/v1/chat/completions`; it selects the first advertised model.

```bash
git clone https://github.com/Thyrz73/llama-cpp-document-processor.git
cd llama-cpp-document-processor
cp .env.example .env
cp config/system_prompt.example.txt config/system_prompt.txt
mkdir -p data
```

Edit `.env`:

```dotenv
LLAMA_BASE_URL=http://host.docker.internal:8080
LLAMA_API_KEY=
BIND_ADDRESS=127.0.0.1
APP_PORT=9932
CONTEXT_TOKENS=16384
PUID=1000
PGID=1000
```

Set `PUID` and `PGID` to the owner of `data/` (`id -u` and `id -g` on Linux). The directory must be writable by that user:

```bash
sudo chown -R "$(id -u):$(id -g)" data
chmod 700 data
chmod 600 .env config/system_prompt.txt
docker compose config --quiet
docker compose up -d --build
curl -f http://127.0.0.1:9932/api/health
```

Open **http://127.0.0.1:9932** and try a small text PDF. Health checks confirm the web app responds; they do not check the model or worker.

`LLAMA_BASE_URL` must be reachable **from the container**. The host alias works when llama.cpp listens on the Docker host's gateway interface. Use its LAN IP if needed; `localhost` inside the container points to the processor itself.

For access from other trusted LAN devices, set `BIND_ADDRESS` to the host's LAN IP and run `docker compose up -d`. Adjust the URLs above for your address and port. There is no built-in authentication.

### Shared Docker network

If llama.cpp is only accessible on an existing Docker network, add to `.env`:

```dotenv
LLAMA_DOCKER_NETWORK=your_llama_network
LLAMA_BASE_URL=http://your-llama-service:8080
```

Use the service name and internal port, then start with:

```bash
docker compose -f compose.yaml -f compose.network.example.yaml up -d --build
```

Keep these same `-f` options for later Compose commands. The base installation needs no external Docker network.

## Using the app

1. Upload a PDF and enter a request (maximum 1,500 characters).
2. Choose a profile, report language and detail level. Review the applied settings, then start.
3. Follow progress or open live output. Closing the browser does not stop the analysis.
4. Choose **View** to read the formatted report in a new tab, or **Download** to save the original Markdown.

- **Cancel** stops a queued analysis at once and a running one at its next safe point.
- **Retry** restarts a failed or cancelled analysis with its original settings and reuses its finished steps. Create a new analysis to change settings or prompts.
- **History** keeps every analysis; finished ones can be deleted one by one or in bulk.

See [using the interface in detail](docs/usage.md) for report names, the reader, live output and history.

## How it works

```text
PDF → text extraction / selective OCR → sections with [Page N] markers
    → factual notes → consolidation when needed → Markdown report
```

PyMuPDF extracts text, pypdf provides a fallback, and Tesseract handles pages with insufficient usable text. English/French OCR packs are included in the image. Password-protected PDFs must be unlocked first. Reports list OCR pages and pages with insufficient text; a PDF with no usable text fails.

The current task prompts in [document_processor/prompts.py](document_processor/prompts.py) ask the model to:

- Separate observation, correlation, causal claims and unverified hypotheses.
- Distinguish real contradictions from forecast/observation gaps and missing information.
- Check citations against source page markers during section analysis, then preserve them through consolidation and synthesis.
- Keep figures, units, assumptions, limitations and the source's degree of certainty. “Not mentioned” does not mean “not performed”.

Page markers use PDF page positions starting at 1, which may differ from printed page numbers. Synthesis receives notes, not the original PDF. These instructions improve guidance; they do not automatically verify factual accuracy.

Edit the private `config/system_prompt.txt` for installation-wide instructions. The example file is a starting point: updates do not overwrite your private copy. Each new job saves its system prompt and task templates. **Existing jobs, including retries, keep their saved prompts.**

## Configuration

Profiles change application token budgets. They do not change model, threads, GPU, slots or KV cache. All remain experimental and sequential.

| Profile | Section input | Analysis output | Merge output | Final output |
| --- | ---: | ---: | ---: | ---: |
| `low` | 3000 | 3072 | 2048 | 3072 |
| `medium` | 4000 | 3584 | 3072 | 4096 |
| `high` | 5500 | 4096 | 4096 | 6144 |
| `ultra` | 7000 | 6144 | 6144 | 8192 |

Values come from [profiles.py](document_processor/profiles.py). Concise detail multiplies the final budget by 0.8; detailed multiplies it by 1.5. Advanced overrides apply last. Larger budgets do not guarantee better or faster results.

Compose reads `.env`. These settings configure the installation; the token budgets apply when no profile is selected. **The interface's Low profile replaces all four token budgets.**

| Variable | Compose default | Purpose |
| --- | --- | --- |
| `LLAMA_BASE_URL` | `http://host.docker.internal:8080` | Model API address |
| `LLAMA_API_KEY` | empty | Optional backend key |
| `BIND_ADDRESS` / `APP_PORT` | `127.0.0.1` / `9932` | Published interface and port |
| `LOG_LEVEL` | `INFO` | Python application logging: `ERROR`, `WARNING`, `INFO`, `DEBUG` |
| `PUID` / `PGID` | `1000` / `1000` | Container user and group |
| `CONTEXT_TOKENS` | `16384` | Application context ceiling; stay within the engine's capacity |
| `CHUNK_TOKENS` | `3000` | Section input budget |
| `ANALYSIS_TOKENS` / `MERGE_TOKENS` / `FINAL_TOKENS` | `1000` / `1200` / `1800` | Output budgets without a profile |
| `OUTPUT_LANGUAGE` | `auto` | Default report language; per-job selection takes precedence |
| `MAX_PAGES` / `MAX_UPLOAD_MB` | `120` / `20` | Page count and upload size in MiB |
| `REQUEST_TIMEOUT_SECONDS` | `21600` | Streaming read/inactivity timeout |
| `STEP_TIMEOUT_SECONDS` | `21600` | Deadline per generation, including silent waiting |
| `MAX_MODEL_CALLS` / `MAX_JOB_ATTEMPTS` | `500` / `10` | Generation calls per attempt / total attempts per job |
| `OCR_ENABLED` / `OCR_LANGUAGES` | `true` / `eng+fra` | Selective OCR and language packs |
| `OCR_DPI` / `OCR_MAX_PIXELS` | `150` / `5000000` | Render resolution and pixel limit; resolution is reduced as needed |
| `OCR_TIMEOUT_SECONDS` | `180` | Timeout per OCR page |
| `LIVE_OUTPUT_ENABLED` / `LIVE_PREVIEW_CHARS` | `true` / `12000` | Saved preview, limited to 1,000–50,000 characters per channel |
| `PROCESSOR_MEMORY_LIMIT` / `PROCESSOR_CPUS` | `512m` / `0.75` | Processor container limits; no additional swap allowance |
| `LLAMA_USAGE_SUPPORTED` | `false` | Request streamed usage when verified supported |
| `LLAMA_APPLY_TEMPLATE_SUPPORTED` | `false` | Size prompts with the backend's rendered chat template |
| `LLAMA_THINKING_ADAPTER` | `unsupported` | Reasoning control: `unsupported`, `chat_template` or `prompt` |
| `LLAMA_INITIAL_THINKING` | `false` | Parse initial text as reasoning when the verified template requires it |

Compose fixes `DATA_DIR=/app/data`, `SYSTEM_PROMPT_FILE=/app/config/system_prompt.txt` and `OMP_THREAD_LIMIT=1`. `LLAMA_DOCKER_NETWORK` is only used by the optional network overlay. See [.env.example](.env.example) and [compose.yaml](compose.yaml).

After environment changes, run `docker compose up -d`; `restart` keeps the old environment. Running `python app.py` directly does not load `.env`; unset or empty variables then use the Compose defaults above.

Settings are validated at startup. A non-integer or out-of-range number, a boolean other than `true/false/1/0/yes/no/on/off`, an unknown reasoning adapter, a non-`http(s)` `LLAMA_BASE_URL` or budgets that exceed `CONTEXT_TOKENS` stop the processor with a `Configuration error: …` message naming the problem; check `docker compose logs`.

Reasoning controls require a verified model/template adapter. `chat_template` sends `enable_thinking`; `prompt` adds `/think` or `/no_think`. Hiding reasoning in the interface does not disable its generation. See [engine tuning](docs/engine-tuning.md).

## Data, updates and backups

```text
data/jobs.sqlite3                     # Jobs, private settings, attempts and metrics
data/jobs/<id>/
  source.pdf
  artifacts/<configuration-hash>/     # Extracted text, chunks, notes, merges, report and manifest
  live_preview.json                   # Latest bounded output and emitted reasoning
  rapport_final.md                    # Published report
  previous_reports/                   # Earlier published reports, when replaced
```

- After a restart, interrupted analyses return to the queue unless their cancellation was requested. Compatible finished steps are reused after dependency and file-hash checks.
- Old jobs without saved settings are recomputed with a warning. Files and metrics are kept until the analysis is deleted from the history; there is no automatic cleanup.
- See [artifact growth and retention](docs/storage-retention.md) for the storage tradeoffs and a future retention proposal that remains disabled by default.
- Startup applies numbered SQLite migrations ([database.py](document_processor/database.py)): each runs once, in its own transaction, and only adds tables, columns or indexes. A database already upgraded by a newer version stops startup with a `Database error: …` message; restore a matching backup before downgrading.
- Preserve `.env`, `config/system_prompt.txt` and `data/` across updates. Stop all processor instances before a raw copy and keep any SQLite WAL/SHM files with the database. Shutdown attempts a checkpoint but cannot guarantee that `jobs.sqlite3` alone contains every committed change if another writer remains active or the checkpoint is incomplete. Back up the old source or image too if you may need to roll back, and follow [migration and rollback](docs/migration-v2.md#update) for coherent backup options.
- Never commit private configuration, documents or reports. The supplied ignore files exclude common private paths; check what you stage.

## Troubleshooting and limits

Logs go to `docker compose logs -f`; they never contain PDF text, prompts or model output. See [logs, metrics and checks](docs/usage.md#logs-metrics-and-checks).

```bash
docker compose ps
docker compose logs -f document-processor
docker stats --no-stream
curl -f http://127.0.0.1:9932/api/health
```

- **Database errors:** check `data/` ownership and write permissions.
- **Backend unavailable:** check the container's access to `LLAMA_BASE_URL` and the optional key.
- **Context or output limit:** inspect applied settings and finish reason. Start a new job with suitable budgets; Retry preserves the old ones. Truncated output fails, with no automatic continuation.
- **OCR failure:** check language packs, page complexity and container memory. Large PDFs may exceed memory limits despite small upload sizes.
- **Garbled accents:** see [UTF-8 handling and old reports](docs/utf8-correction.md).
- **Empty reasoning panel:** the backend may not emit it. Live output shows only the current/latest pass and may be incomplete.

Known limits:

- OCR recognizes text; it does not interpret charts or images. Columns and tables may extract in the wrong order.
- Model output can omit or invent facts: check important claims and citations against the PDF. Automatic checks are warnings, not proof.
- There are no accounts or per-user access controls. Anyone who can reach the app can read every job, report and requested live reasoning: keep it private or behind an authenticated reverse proxy.

## Development

Contributions are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) describes the workflow, the checks and the commit conventions, and the [architecture guide](docs/architecture.md) explains the code structure, the life of a job and where to make common changes.

Docker and CI use Python 3.12. On Ubuntu/Debian, install `python3-venv`, `tesseract-ocr`, `tesseract-ocr-eng` and `tesseract-ocr-fra`, then:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m unittest discover -s tests -v
node tests/test_polling.js
```

Python tests use temporary data and simulated model responses; no llama.cpp is needed. One test starts `app.py` on a free local port and stops it with `SIGTERM`. Two tests require real Tesseract: they are skipped locally when it is missing, but never in CI (`CI` set), where a missing Tesseract fails the build. Node.js 22.7 or later is needed only for the frontend tests (CI uses Node.js 22). [CI](.github/workflows/tests.yml) also lints with ruff and type-checks with mypy (settings in [pyproject.toml](pyproject.toml)), checks syntax, an offline benchmark plan, Compose and Docker packaging.

Dependencies are hash-locked and the Docker base image is pinned by digest; [Dependabot](.github/dependabot.yml) proposes weekly updates on `develop`. To change a dependency, see [updating dependencies](CONTRIBUTING.md#updating-dependencies).

## Documentation

- [Contributing](CONTRIBUTING.md): workflow, checks, commit conventions and dependency updates.
- [Architecture](docs/architecture.md): modules, data flow, design rules and common changes.
- [Using the interface in detail](docs/usage.md): history, reports, cancellation, live output, logs and metrics.
- [API](docs/api.md): routes, statuses, errors, retries and history deletion.
- [Engine tuning and benchmarks](docs/engine-tuning.md).
- [Documentary reliability](docs/documentary-reliability.md): what the automatic checks verify, and their limits.
- [UTF-8 handling](docs/utf8-correction.md) and [migration and rollback](docs/migration-v2.md).

Maintained by [Guillaume Luquet / Thyrz73](https://github.com/Thyrz73). Report bugs through [GitHub issues](https://github.com/Thyrz73/llama-cpp-document-processor/issues), with the revision and relevant logs, excluding secrets and document content.

MIT license — see [LICENSE](LICENSE).
