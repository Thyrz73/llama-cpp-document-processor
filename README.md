# Document Processor for llama.cpp

A self-hosted PDF analysis app using an **existing llama.cpp server**. Upload a PDF, ask a question and view or download a Markdown report with page references. One worker processes one document at a time.

The app includes English/French interface controls, report language and detail settings, selective OCR, live output, cancellation and saved progress. It does not install a model or reconfigure llama.cpp.

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

Active analyses appear first in a highlighted section; history appears below a separate heading. Saved custom analysis instructions are available in a collapsible block on each job, during processing and in history. Line breaks and the complete submitted text are preserved. Default instructions do not create a custom-prompt block. Older jobs show their saved request with a neutral label when its origin is unknown; server system prompts and task templates stay private.

Reports use a deterministic descriptive download name such as `Consommation_energetique_des_batiments_2025-2026_rapport.md`, derived from an existing Markdown heading. Generic headings are skipped; the fallback is `<original PDF stem>_rapport.md`. Names use portable ASCII and stay within 100 characters. No model call or PDF reanalysis is needed. The internal `rapport_final.md` path stays unchanged, and older reports remain supported.

The server-rendered reader supports headings, lists, tables, quotes, links and code, with selectable Unicode text and the saved light/dark theme. Raw HTML is escaped, executable link schemes are rejected and images are disabled. A restrictive Content Security Policy blocks embedded resources. Preview is limited to **1 MiB**; larger files remain downloadable without loading the full file into application memory. Empty reports show an explicit message and can still be downloaded. Only completed final reports are exposed; private configurations and intermediate pipeline files are not published.

**Cancel** stops queued jobs immediately and requests cancellation of running work. OCR finishes its current subprocess first; a remote model may take longer to stop. **Retry** is available for failed or cancelled jobs and keeps their original settings. Create a new analysis to change budgets, language or prompts.

If a cancelled or timed-out HTTP reader is still closing, the next generation waits for it instead of failing subsequent jobs. This wait remains cancellable and does not consume the next generation's time limit. A response arriving after cancellation is closed before reading its body.

The interface initially selects `low`, automatic report language and standard detail. Interface language and theme are saved in the browser; changing them does not translate existing reports. Automatic report language follows the request, not the interface language.

Jobs refresh every six seconds while work is active, with backoff on errors. Live output refreshes every 1.7 seconds while its panel is open. Both pause when the tab is hidden. The list shows the latest 100 jobs; older jobs remain stored.

## Analysis and prompts

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

## Profiles and configuration

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

## Saved data and updates

```text
data/jobs.sqlite3                     # Jobs, private settings, attempts and metrics
data/jobs/<id>/
  source.pdf
  artifacts/<configuration-hash>/     # Extracted text, chunks, notes, merges, report and manifest
  live_preview.json                   # Latest bounded output and emitted reasoning
  rapport_final.md                    # Published report
  previous_reports/                   # Earlier published reports, when replaced
```

On restart, interrupted running jobs return to the queue unless cancellation was requested. Compatible completed steps are reused after dependency and file-hash checks. Failed/cancelled jobs wait for Retry. Old jobs without saved settings are recomputed with a warning. Files and metrics have no automatic cleanup, and there is no delete action.

Startup applies an additive, idempotent SQLite migration for the descriptive report name and custom-instructions indicator. Existing request text is reused; no private prompt is copied into the public configuration. New names are saved at finalization; old report names are calculated and cached only on first view/download. Listing history does not read Markdown contents.

Preserve `.env`, `config/system_prompt.txt` and `data/` across updates. Stop the processor before copying SQLite and its WAL/SHM files. Back up the old source/image too if you need rollback. Follow [migration and rollback](docs/migration-v2.md).

Never commit private configuration, documents or reports. The supplied ignore files exclude common private paths; check what you stage.

## Troubleshooting and limits

Application events go to stdout/stderr for `docker compose logs -f`: startup, recovery, submissions, extraction/OCR, section/merge/final steps, reuse, cancellation and report access. Events include job IDs, durations measured with a monotonic clock, progress and token usage when the backend supplies it. Logs contain neither PDF text nor full prompts/model responses. Tracebacks retain frame locations while omitting source lines and private exception messages. Streaming updates are never logged, including at DEBUG. Existing on-disk generation diagnostics remain separate. Docker log rotation stays under deployment control.

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

OCR recognizes text; it does not interpret charts or images. Columns and tables may extract in the wrong order. Model output can omit or invent facts: check important claims and citations against the PDF.

There are no accounts or per-user access controls. Anyone with application access can read jobs, reports and explicitly requested live reasoning. Keep the service private or use an authenticated reverse proxy.

Metrics report application durations and optional backend usage/timings. Unknown values stay unknown; first-event latency is not engine prefill time. Numeric/page-reference counts are warnings, not factual scores. See [API details](docs/api.md) and [benchmark instructions](docs/engine-tuning.md).

Documentary checks retain physical page/passage provenance separately from model notes. They flag nonexistent pages, some exact-quotation page mismatches and possible entity/delta errors in explicitly structured numeric rows. Source reserves and figures are carried through merges in a bounded evidence block; omitted extracts remain in private `source_evidence.json`. Detailed checks are saved in `quality_checks.json`, and warnings appear in the downloaded report. These checks neither repair numbers automatically nor verify scientific correctness or every citation. All four profiles use the same checks without an additional LLM pass. See the [implementation, limits and validation report](docs/documentary-reliability.md).

## Development

Docker and CI use Python 3.12. On Ubuntu/Debian, install `python3-venv`, `tesseract-ocr`, `tesseract-ocr-eng` and `tesseract-ocr-fra`, then:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m unittest discover -s tests -v
node tests/test_polling.js
```

Python tests use temporary data and simulated model responses; no llama.cpp is needed. Two tests require real Tesseract: they are skipped locally when it is missing, but never in CI (`CI` set), where a missing Tesseract fails the build. Node.js is needed only for frontend tests; CI uses Node.js 22. [CI](.github/workflows/tests.yml) also lints with ruff and type-checks with mypy (settings in [pyproject.toml](pyproject.toml)), checks syntax, an offline benchmark plan, Compose and Docker packaging.

Dependencies are declared with version ranges in `requirements.in` (runtime) and `requirements-dev.in` (tools). The installed `requirements.txt` and `requirements-dev.txt` files are hash-locked for Linux and Python 3.12; never edit them by hand. After changing a `.in` file, regenerate both locks with pip-tools, runtime first:

```bash
.venv/bin/python -m pip install pip-tools
.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url -o requirements.txt requirements.in
.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url -o requirements-dev.txt requirements-dev.in
```

The Docker base image is pinned by digest. [Dependabot](.github/dependabot.yml) proposes weekly updates for Python packages, the base image and GitHub Actions on `develop`.

Maintained by [Guillaume Luquet / Thyrz73](https://github.com/Thyrz73). Report bugs through [GitHub issues](https://github.com/Thyrz73/llama-cpp-document-processor/issues), with the revision and relevant logs, excluding secrets and document content.

MIT license — see [LICENSE](LICENSE).
