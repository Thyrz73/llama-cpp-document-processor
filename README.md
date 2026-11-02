# Document Processor for llama.cpp

A lightweight, self-hosted PDF analysis app powered by an **existing llama.cpp server**. Upload a PDF, ask a question, and download a Markdown report with page references. One background worker handles the queue sequentially.

**No extra LLM, GPU, vector database, Redis, or Open WebUI required.** Designed to work on modest homelab hardware.

> **Status:** Personal project / early release. Report quality depends on the PDF and model; designed for a trusted homelab, not a public multi-user service.

## Features

- **Drag-and-drop web interface** for digital and scanned PDFs, with **English/French UI**, local flag icons, and **System / Light / Dark** themes (saved per browser).
- **Text extraction and selective OCR:** PyMuPDF, pypdf fallback, then Tesseract for pages with insufficient text. English/French OCR packs are included.
- **Four performance profiles:** limited, moderate, high and workstation; preview effective budgets and use validated advanced overrides. Profiles are experimental, remain sequential, and never reconfigure llama.cpp.
- **Document-wide analysis:** cached llama.cpp tokenization, compact factual notes, context-aware consolidation and a final Markdown report. Report language and detail are independent of the resource profile.
- **Progress and live output:** translated phases, bounded previews, explicit reasoning opt-in, request deadlines, cancellation and per-attempt/per-step metrics. Unknown engine metrics remain unknown.
- **Persistent work:** SQLite job queue and atomic intermediate files; interrupted jobs resume after a restart. Immutable snapshots and verified artifact manifests prevent mixing incompatible saved steps. Historical files are preserved.
- **Central analysis instructions:** editable `config/system_prompt.txt`, captured privately with each new job. Subsequent edits affect only new jobs.

## How it works

```text
PDF -> text extraction / selective OCR -> token-bounded sections
    -> llama.cpp section analyses -> grouped consolidation -> Markdown report
```

The browser talks to Document Processor, which stores jobs locally and sends extracted text and instructions to `LLAMA_BASE_URL`. With a local llama.cpp server, inference stays on your infrastructure. "OpenAI-compatible" describes the API format; no OpenAI account is required. Uploads in llama.cpp's own chat UI do not pass through this app.

## Requirements

- Git, Docker Engine and the Docker Compose plugin.
- An already running **llama.cpp HTTP server** reachable from the processor container, with a loaded chat model and `/tokenize`, `/v1/models`, and **streaming** `/v1/chat/completions` support. The processor selects the first model returned by `/v1/models`.
- Sufficient available RAM for llama.cpp **plus** the processor. The default processor memory limit is **512 MiB**, but some PDFs may need more.
- Internet access during Docker builds to download the base image, Python packages and Tesseract when they are not cached.

No model files are downloaded by this repository.

## Quick start

```bash
git clone https://github.com/Thyrz73/llama-cpp-document-processor.git
cd llama-cpp-document-processor
cp .env.example .env
cp config/system_prompt.example.txt config/system_prompt.txt
mkdir -p data
```

Edit `.env`:

```dotenv
# Address reachable FROM the processor's Docker container
LLAMA_BASE_URL=http://host.docker.internal:8080
# Leave empty if llama.cpp has no API key
LLAMA_API_KEY=

# Keep the web app on localhost by default
BIND_ADDRESS=127.0.0.1
APP_PORT=9932

# Match, or stay below, your llama.cpp server's actual context size
CONTEXT_TOKENS=16384
CHUNK_TOKENS=3000
```

The **host gateway example** works when llama.cpp is published on the Docker host and reachable at its gateway interface. If llama.cpp is only bound to a specific LAN IP, replace `LLAMA_BASE_URL` with `http://<that-LAN-IP>:<llama-port>`.

Set the UID and GID that own `./data` in `.env` (`id -u`, `id -g` on Linux). The defaults are `1000:1000`. Fix ownership if needed:

```bash
sudo chown -R "$(id -u):$(id -g)" data
chmod 700 data
chmod 600 .env config/system_prompt.txt
```

Then start the processor:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -f http://127.0.0.1:9932/api/health
```

Open **http://127.0.0.1:9932** on the Docker host and upload a small, text-based PDF first. To allow other devices on a trusted LAN to connect, set `BIND_ADDRESS` to the Docker host's LAN IP (for example `192.0.2.10`) and restart via `docker compose up -d`.

Adjust example URLs when changing `BIND_ADDRESS` or `APP_PORT`. `/api/health` only confirms that the web API responds; test a PDF to check the full pipeline.

### If llama.cpp is on a shared Docker network

For a server not published on a host interface, the optional `compose.network.example.yaml` connects the processor to an existing external network:

```dotenv
LLAMA_DOCKER_NETWORK=your_llama_network
LLAMA_BASE_URL=http://your-llama-service:8080
```

```bash
docker compose -f compose.yaml -f compose.network.example.yaml up -d --build
```

The hostname must match the reachable service/container network alias, and the port must match **llama.cpp's internal listening port**. No particular Docker network name is required by this repository.

When using this overlay, include `-f compose.yaml -f compose.network.example.yaml` in subsequent Compose commands, including updates, logs, status and shutdown.

## Using the app

1. Select or drop a PDF and enter your analysis request (up to 1,500 characters).
2. Select a **performance profile**, report language and detail level; inspect the resolved settings and warnings. Optional advanced fields override only application parameters. Select **Start analysis**. Jobs move through `queued` and `running`; only one document is processed at a time.
3. Optionally open **Show live output** to inspect the latest model pass. Closing the browser does not stop the worker.
4. When the job is **Completed** (`done`, 100%), select **Download Markdown report** to save `analysis_<first 8 job-ID characters>.md`. Failed or cancelled jobs show a **Retry** button. Retry keeps the same snapshot; create a new job to change settings. The **Metrics** button shows attempts and individual steps. **Cancel** stops queued work immediately and running work cooperatively.

PDFs requiring a password must be unlocked before upload. Page limits are checked during processing. Reports identify pages with insufficient extracted/OCR text; a document with no usable text fails.

The job list polls every six seconds while any job is queued or running, pauses in hidden tabs and refreshes when the tab becomes visible. **Refresh** also works when polling has stopped. The UI lists the latest 100 jobs; older active jobs still keep polling enabled, and older files remain on disk.

## Configuration

Deployment settings are read from `.env` by Compose (see [.env.example](.env.example)). Defaults come from [compose.yaml](compose.yaml), except the optional network setting. Running `python app.py` directly does not load `.env` automatically.

| Variable                  | Default                            | Description                                                                                                                                                                                        |
| ------------------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LLAMA_BASE_URL`          | `http://host.docker.internal:8080` | llama.cpp API URL reachable from the processor container; use its service name/IP and port.                                                                                                        |
| `LLAMA_API_KEY`           | empty                              | Optional bearer token for llama.cpp; leave empty if authentication is not required.                                                                                                                |
| `BIND_ADDRESS`            | `127.0.0.1`                        | Host interface on which Docker publishes the web port. Use a LAN IP only when LAN access is intended.                                                                                              |
| `APP_PORT`                | `9932`                             | Port used by the Flask/Waitress application inside the container and published by Docker.                                                                                                          |
| `PUID`                    | `1000`                             | Container user ID; match the owner of the host `./data` directory.                                                                                                                                 |
| `PGID`                    | `1000`                             | Numeric group ID used to run the container and write `./data`.                                                                                                                                     |
| `CONTEXT_TOKENS`          | `16384`                            | Input/output context budget, no greater than llama.cpp's actual context. Prompts and outputs need headroom; an insufficient budget is rejected.                                                    |
| `CHUNK_TOKENS`            | `3000`                             | Maximum document-text tokens sent to one section analysis. Validated range: `128`–`131072`, additionally constrained by the available context and output budget.                                   |
| `ANALYSIS_TOKENS`         | `1000`                             | Maximum generated tokens for each document-section analysis.                                                                                                                                       |
| `MERGE_TOKENS`            | `1200`                             | Maximum generated tokens for each intermediate consolidation pass.                                                                                                                                 |
| `FINAL_TOKENS`            | `1800`                             | Maximum generated tokens for the final Markdown report.                                                                                                                                            |
| `OUTPUT_LANGUAGE`         | `auto`                             | Output language. `auto` follows the user's request; otherwise use a language name such as `English` or `French`.                                                                                   |
| `LIVE_OUTPUT_ENABLED`     | `true`                             | Saves/exposes the bounded live preview. Disabling it does not disable LLM streaming. True values: `1`, `true`, `yes`, `on` (case-insensitive); use `false` to disable.                             |
| `LIVE_PREVIEW_CHARS`      | `12000`                            | Recent characters retained per preview channel, clamped to `1000`–`50000`.                                                                                                                         |
| `MAX_PAGES`               | `120`                              | Maximum number of PDF pages accepted for one job.                                                                                                                                                  |
| `MAX_UPLOAD_MB`           | `20`                               | Maximum uploaded PDF size in MiB.                                                                                                                                                                  |
| `REQUEST_TIMEOUT_SECONDS` | `21600`                            | Read/inactivity timeout in seconds for streamed generations, not a generation/job deadline. Tokenizer/model-list requests use a separate 90-second read timeout; connection timeout is 10 seconds. |
| `OCR_ENABLED`             | `true`                             | Enables OCR on pages whose extracted text is insufficient. Accepted true values: `1`, `true`, `yes`, `on`.                                                                                         |
| `OCR_LANGUAGES`           | `eng+fra`                          | Tesseract language pack(s) used for OCR, separated with `+`. The image includes English (`eng`) and French (`fra`).                                                                                |
| `OCR_DPI`                 | `150`                              | OCR rendering resolution; higher values can improve recognition at greater CPU/memory cost.                                                                                                        |
| `OCR_MAX_PIXELS`          | `5000000`                          | Target pixel budget per OCR page. Rendering resolution is reduced to fit, and rejects pages that cannot fit even at minimum rendering resolution.                                                  |
| `OCR_TIMEOUT_SECONDS`     | `180`                              | Maximum OCR time allowed for one page.                                                                                                                                                             |
| `PROCESSOR_MEMORY_LIMIT`  | `512m`                             | Docker memory limit for the processor container. `memswap_limit` is set to the same value, so swap is not added.                                                                                   |
| `PROCESSOR_CPUS`          | `0.75`                             | Docker CPU quota for the processor container. Tesseract is also limited to one OpenMP thread by Compose.                                                                                           |
| `LLAMA_DOCKER_NETWORK`    | unset                              | Name of an existing external Docker network containing llama.cpp. Used only with `compose.network.example.yaml`; required by that optional overlay.                                                |

Compose fixes `DATA_DIR=/app/data`, `SYSTEM_PROMPT_FILE=/app/config/system_prompt.txt`, and `OMP_THREAD_LIMIT=1`. Adding these names to `.env` alone has no effect. The paths match the `./data` and read-only `./config` mounts; changing them requires matching Compose changes.

After editing `.env`, wait until the queue is idle and run `docker compose up -d` to apply changes. `docker compose restart` alone keeps the old container environment.

Start with conservative token budgets. An API request without `options` keeps the historical deployment defaults. An explicit profile replaces its section/analysis/merge defaults, then validated advanced overrides apply. All four profiles initially use one worker and one generation at a time; `ultra` does not enable parallel inference.

The current experimental profile definitions are centralized in `document_processor/profiles.py`: low `3000/3072/2048/3072`, medium `4000/3584/3072/4096`, high `5500/4096/4096/6144`, ultra `7000/6144/6144/8192` (section/analysis/merge/final tokens). They are **not calibrated hardware recommendations**. Final output is adjusted by the separate detail choice (concise ×0.8, standard ×1, detailed ×1.5); an explicit final-output override wins. No speed gain or exact ETA is promised.

Changing environment variables, language, prompts or profile does not alter existing jobs. **Retry uses the original snapshot. Create a new analysis to change settings**, including after a `length` failure. Snapshots include private prompt text; protect the data volume like the source PDFs.

| Additional variable              | Default       | Purpose                                                                                                                                                                    |
| -------------------------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `STEP_TIMEOUT_SECONDS`           | `21600`       | Actual generation deadline, including a silent prefill; distinct from the socket inactivity timeout.                                                                       |
| `MAX_MODEL_CALLS`                | `500`         | Maximum generation calls per processing attempt.                                                                                                                           |
| `MAX_JOB_ATTEMPTS`               | `10`          | Maximum manual/restart attempts accepted for a job; no automatic generation retries.                                                                                       |
| `LLAMA_USAGE_SUPPORTED`          | `false`       | Request streaming usage only after verifying backend support. Optional returned usage/timings are parsed regardless.                                                       |
| `LLAMA_APPLY_TEMPLATE_SUPPORTED` | `false`       | Use `/apply-template` for input sizing after verifying it matches completion templating and reasoning options. Otherwise use tokenization plus a marked template estimate. |
| `LLAMA_THINKING_ADAPTER`         | `unsupported` | `unsupported`, `chat_template`, or `prompt`. Enable only after verifying the exact GGUF/build/template.                                                                    |
| `LLAMA_INITIAL_THINKING`         | `false`       | Start the parser inside a reasoning block when the verified template opens `<think>` without emitting it.                                                                  |

The thinking adapter controls whether advanced per-stage `inherit/on/off` policies are available. `chat_template` sends `chat_template_kwargs.enable_thinking`; `prompt` uses `/think` or `/no_think`. The default inherits engine behavior. **Masking reasoning in the interface does not reduce inference work.** Never assume a Qwen3 derivative supports the official model's controls.

See [engine tuning and measurement](docs/engine-tuning.md), [API contracts](docs/api.md), and [migration/rollback](docs/migration-v2.md).

### Customize the system prompt

Edit `config/system_prompt.txt`, copied in the quick start, to set processor-wide analysis instructions. It is Git-ignored and affects only requests sent by this app. Set `OUTPUT_LANGUAGE=French` (or `auto`) in `.env` to choose the output language separately.

### Themes and live model output

The **System / Light / Dark** theme preference is stored in the browser. **Generated text** shows the current or latest section, merge or final-report pass. **Reasoning (if available)** is opt-in and displays only emitted text; it may be absent or incomplete. The API omits this channel unless explicitly requested. Interface language changes do not translate or regenerate model output.

The progress bar covers extraction, section analysis, merging and final writing; it is a workflow estimate, not a time estimate. During section analysis, the Live panel's phase label includes PDF page numbers.

Live output polls separately every 1.7 seconds, with bounded request timeouts, while its panel is open and the tab is visible, including for completed jobs. Jobs polling backs off after repeated failures; hidden tabs do not start polling requests. Each new model pass replaces the bounded preview; full successful notes and reports remain saved separately. Streaming support is required even with `LIVE_OUTPUT_ENABLED=false`.

## Persistent data

```text
data/                           # Private, mounted at /app/data
├── jobs.sqlite3                 # Jobs, immutable configurations, attempts and steps
└── jobs/<job-id>/
    ├── source.pdf
    ├── artifacts/<snapshot-hash>/
    │   ├── manifest.json        # Dependencies and SHA-256 checks
    │   ├── extracted.json
    │   ├── chunks.json
    │   ├── analysis_0000.md
    │   ├── merge_00_0000.md
    │   └── rapport_final.md
    ├── live_preview.json        # Bounded latest output; private
    ├── previous_reports/       # Preserved earlier reports, when replaced
    └── rapport_final.md        # Same download path/name as before
```

**Never commit `.env`, `config/system_prompt.txt`, `data/`, uploaded PDFs, SQLite databases, or real analysis reports.** `.gitignore` and `.dockerignore` exclude these common files, but always inspect `git status` before pushing.

On restart, interrupted jobs return to the queue and reuse compatible saved files. Failed/cancelled jobs wait for **Retry**. Old jobs without a snapshot are recomputed with a visible historical-configuration warning, while their original files remain intact. A newly uploaded PDF creates a new job, even if it was uploaded before. Files and metrics are retained without automatic cleanup. Metric API pages are bounded. There is a cancel action but no delete action. During an OCR subprocess, cancellation takes effect at the next page boundary; during silent model generation, the application checks cancellation/deadlines independently of incoming tokens. A closing HTTP reader blocks further local generations until it exits; stopping inference inside a remote backend ultimately depends on that backend.

## Measurements and controlled comparisons

Per-job metrics separate queue waiting, extraction/OCR, tokenization, sections, merges, final generation and saving. Engine usage, prefill/decode timings and cache counts are recorded only when available. First-event latency includes transport/waiting and is **not** labeled prefill. Character counts and SSE events are never labeled tokens. Mechanical checks of page references and numeric items are warning signals, not factual-quality scores.

No benchmark runs at startup or during diagnostics. Prepare an offline plan with:

```bash
.venv/bin/python scripts/benchmark.py --variants docs/benchmark-variants.example.json
```

Execution requires `--execute`, an authorized PDF, a question file and a new output path. Read [the benchmark protocol](docs/engine-tuning.md) before executing. The tool creates fresh jobs, alternates trial order, exports non-content metrics and supports an optional mechanical recall checklist. Human review of facts, units, contradictions and citations remains required.

## Updating and backups

Wait until no job is queued or running. With a clean Git worktree on the branch you intend to use, stop the processor, back up its state, then update:

```bash
docker compose stop document-processor
umask 077
tar -czf "../document-processor-backup-$(date +%Y%m%d-%H%M%S).tar.gz" \
  .env config/system_prompt.txt data
```

Only after the backup succeeds, update the source and recreate the processor:

```bash
git pull --ff-only && docker compose up -d --build --no-deps document-processor
```

The backup contains private documents and possibly credentials; store it securely. Stopping before copying `data/` keeps SQLite consistent. Preserve `.env`, `config/system_prompt.txt` and `data/` across updates. For a normal stop/start, use `docker compose stop document-processor` / `docker compose up -d`.

## Monitoring and troubleshooting

```bash
docker compose ps
docker compose logs -f document-processor
docker stats --no-stream
curl -i http://127.0.0.1:9932/api/health
```

- **SQLite `unable to open database file`:** make sure `./data` is writable by `PUID:PGID` and that it is a directory, not a file.
- **Cannot connect to llama.cpp:** check `LLAMA_BASE_URL` from the container's network perspective. `localhost` inside the processor container does **not** mean the Docker host.
- **llama.cpp authorization error:** check the optional `LLAMA_API_KEY` value.
- **Out-of-context / too many tokens:** align `CONTEXT_TOKENS` with your llama.cpp context and reduce `CHUNK_TOKENS` or response budgets.
- **Response hit `max_tokens`:** create a new job with an adjusted output budget while keeping enough context. Retry deliberately preserves the old budget. Truncated generations are treated as failures.
- **No usable text:** check whether OCR is enabled, language packs match the source, and the PDF is not password-protected.
- **Killed during OCR:** check Docker memory usage and increase `PROCESSOR_MEMORY_LIMIT` carefully if the host has headroom.
- **Unexpected output language:** select the report language explicitly and start a new analysis; browser language is independent.
- **Garbled accents such as `Ã©` or `â…`:** update to the UTF-8 streaming correction and start a new analysis. Existing completed reports are preserved. See the [UTF-8 diagnosis, update and controlled recovery guide](docs/utf8-correction.md) before handling old reports.
- **Empty reasoning panel:** the model/server did not emit separate reasoning; this is expected for many models.
- **Live output unavailable:** check that llama.cpp supports `stream: true`, then inspect `docker compose logs`.

## Limitations and security

- Tesseract recognizes printed text, **not the meaning of images, charts, diagrams, or complex layout**. PDF extraction may reorder columns and tables. No vision model is included.
- Small language models may hallucinate or omit facts. **Verify all page references and important claims against the original PDF.**
- No web authentication or per-user isolation. **Do not expose this service to the public Internet.** Binding to a trusted LAN interface does not prevent other LAN users from reading documents or jobs.
- Stored jobs may include sensitive data. Protect host filesystem permissions and back up `data/` appropriately.
- Very large or complex PDFs may exceed Docker memory limits even if their uploaded file size is small.

## Development and tests

Python 3.12 is used in Docker and CI. Install venv support and Tesseract's English/French packs (Ubuntu/Debian: `sudo apt-get install python3-venv tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra`), then run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The tests use temporary documents/databases and simulated model responses; no running llama.cpp is required. Browser polling tests use a simulated DOM/network/timers and run with `node tests/test_polling.js` (Node.js is only needed for that suite; CI uses Node.js 22). [GitHub Actions](.github/workflows/tests.yml) runs both suites on pushes and pull requests.

## Help and contributions

Maintained by [Guillaume Luquet / Thyrz73](https://github.com/Thyrz73). For help or bugs, open a [GitHub issue](https://github.com/Thyrz73/llama-cpp-document-processor/issues) with the Git revision, relevant settings and logs, removing secrets and document content. Pull requests are welcome: describe the change and run the relevant tests.

## License

MIT — see [LICENSE](LICENSE).
