# Document Processor for llama.cpp

A lightweight, self-hosted PDF analysis web app powered by an **existing llama.cpp server**. Drop in a PDF, specify what to look for, and let a single background worker analyze it sequentially. Long documents are split using llama.cpp's tokenizer, intermediate notes are saved, and the results are merged into a Markdown report.

**No extra LLM, GPU, vector database, Redis, or Open WebUI required.** Designed to work on modest homelab hardware.

> **Status:** Small personal project / early release. Extraction, selective OCR, and the analysis pipeline have been exercised locally; behavior and report quality vary by PDF and model. This is not a production multi-user document service.

## Features

- **Drag-and-drop web interface** for digital and scanned PDFs, with **System / Light / Dark** themes (saved per browser).
- **Extract text first:** PyMuPDF, with a pypdf fallback for difficult text layers.
- **Selective OCR:** Tesseract runs only on pages with insufficient extracted text; English and French language packs are included.
- **Sequential LLM processing:** llama.cpp `/tokenize` and OpenAI-compatible `/v1/chat/completions` endpoints; one PDF processing job at a time.
- **Optional live generation view:** expandable, read-only preview of each model pass, including generated text and any **model-emitted** reasoning; the progress bar remains visible. The browser polls a local, bounded snapshot rather than holding the model stream open.
- **Hierarchical consolidation:** extract structured section notes, combine them in batches, and generate a final Markdown report with requested page references.
- **Persistent work:** SQLite job queue and atomic intermediate files; queued/interrupted jobs resume after a restart, and failed jobs can be retried without repeating completed steps.
- **Central analysis instructions:** editable `config/system_prompt.txt` used by this processor, plus a configurable output language.
- **Low overhead:** one small Python service; configurable CPU and memory limits. All model inference remains in your existing llama.cpp server.

## How it works

```text
Browser (upload PDF) --> Document Processor (one Docker container)
                         |-- PyMuPDF -> pypdf fallback -> selective Tesseract OCR
                         |-- llama.cpp tokenizer: split into bounded chunks
                         |-- Stream each model pass -> bounded live preview for UI
                         |-- Analyze chunks sequentially -> Markdown notes
                         |-- Merge notes in batches -> final Markdown report
                         `-- SQLite + disk: persist jobs, PDF, notes and report
                                     |
                                     `--> Existing llama.cpp HTTP API
```

The processor's browser UI is **separate** from llama.cpp's built-in UI. Uploading a PDF in llama.cpp's native chat does not route the request through this processor.

## Requirements

- Docker Engine and the Docker Compose plugin.
- An already running **llama.cpp HTTP server** reachable from the processor container, supporting `/tokenize`, `/v1/models`, and `/v1/chat/completions`.
- Sufficient available RAM for llama.cpp **plus** the processor. The default processor memory limit is **512 MiB**, but some PDFs may need more.
- Internet access **during the first Docker build** to download Python packages and Tesseract. OCR and inference are local afterwards.

No model files are downloaded by this repository.

## Quick start

```bash
git clone https://github.com/YOUR-USERNAME/document-processor.git
cd document-processor
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

# Must match the context configured on your llama.cpp server
CONTEXT_TOKENS=8192
CHUNK_TOKENS=3000
```

The **host gateway example** works when llama.cpp is published on the Docker host and reachable at its gateway interface. If llama.cpp is only bound to a specific LAN IP, replace `LLAMA_BASE_URL` with `http://<that-LAN-IP>:<llama-port>`.

Set the UID and GID that own `./data` in `.env` (`id -u`, `id -g` on Linux). The defaults are `1000:1000`. Fix ownership if needed:

```bash
sudo chown -R "$(id -u):$(id -g)" data
chmod 700 data
chmod 600 .env
```

Then start the processor:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -f http://127.0.0.1:9932/api/health
```

Open **http://127.0.0.1:9932** on the Docker host and upload a small, text-based PDF first. To allow other devices on a trusted LAN to connect, set `BIND_ADDRESS` to the Docker host's LAN IP (for example `192.0.2.10`) and restart via `docker compose up -d`.

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

## Configuration

All settings below are editable in `.env` (see `.env.example`). None are tied to a specific machine or model.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLAMA_BASE_URL` | `http://host.docker.internal:8080` | HTTP address reachable by the container |
| `LLAMA_API_KEY` | empty | Optional bearer token for llama.cpp |
| `BIND_ADDRESS` | `127.0.0.1` | Interface for the host's published web port |
| `APP_PORT` | `9932` | Web/API port |
| `PUID` / `PGID` | `1000` / `1000` | Container's runtime user/group for bind-mounted data |
| `CONTEXT_TOKENS` | `8192` | Must match or be lower than the configured llama.cpp context |
| `CHUNK_TOKENS` | `3000` | Maximum **document-text** tokens per analysis chunk (range 1000–6000) |
| `ANALYSIS_TOKENS` | `1000` | Maximum output tokens per chunk |
| `MERGE_TOKENS` | `1200` | Maximum output tokens per intermediate merge |
| `FINAL_TOKENS` | `1800` | Maximum output tokens for final report |
| `OUTPUT_LANGUAGE` | `auto` | `auto` follows the user's question; or set `English`, `French`, etc. |
| `LIVE_OUTPUT_ENABLED` | `true` | Enable the optional live model-output preview |
| `LIVE_PREVIEW_CHARS` | `12000` | Maximum recent characters retained per preview channel (1000–50000) |
| `PROCESSOR_MEMORY_LIMIT` | `512m` | Docker container's RAM limit |
| `PROCESSOR_CPUS` | `0.75` | Docker CPU quota; Tesseract also uses one thread |
| `MAX_PAGES` | `120` | Maximum pages accepted in one PDF |
| `MAX_UPLOAD_MB` | `20` | PDF file size cap in MiB |
| `REQUEST_TIMEOUT_SECONDS` | `21600` | Timeout for a single LLM request (not a total job deadline) |
| `OCR_ENABLED` | `true` | Enable automatic OCR fallback |
| `OCR_LANGUAGES` | `eng+fra` | Installed OCR languages (English and French) |
| `OCR_DPI` | `150` | Rasterization resolution for OCR |
| `OCR_MAX_PIXELS` | `5000000` | Page render safety limit |
| `OCR_TIMEOUT_SECONDS` | `180` | OCR timeout per page |
| `LLAMA_DOCKER_NETWORK` | unset | Only for the optional external-network Compose overlay |

The token budget reserves room for instructions, chat formatting and output. Larger chunks do **not** necessarily mean faster or more accurate analyses; start conservatively. Changing output instructions or chunk settings mid-job does not retroactively invalidate earlier saved notes.

### Customize the system prompt

Copy `config/system_prompt.example.txt` to `config/system_prompt.txt` (done in the quick start) and edit the latter to define processor-wide analysis behavior. The actual prompt file is deliberately Git-ignored. This prompt is used **only for requests sent by Document Processor**; direct calls to llama.cpp and llama.cpp's built-in browser chat are unaffected.

Avoid committing private instructions or document content. To change output language without editing prompt text, set `OUTPUT_LANGUAGE=French` (or `auto`) in `.env`.

### Themes and live model output

The interface defaults to your operating system's preferred color scheme, and provides a **System / Light / Dark** selector. The choice is saved in your browser's local storage, not on the server.

Under each job, select **Show live output** to inspect the current (or last) model pass:

- **Generated text** shows the section notes, consolidation or final-report text as llama.cpp emits it.
- **Reasoning (if available)** shows only text that the **model itself actually emits**, either as streamed `reasoning_content`/`reasoning` fields or explicit `<think>...</think>` tags in generated content. This is not a reconstruction of unexposed reasoning. Many models, including some fine-tuned or abliterated Qwen variants, may not expose reasoning at all.
- The stage identifies the **document pages** being analyzed (during a section-analysis pass); the progress bar tracks extraction, analysis, merging, and final-report generation.
- The preview is bounded and overwritten for each new model pass. Successful **section notes, merges, and reports** are still saved separately and are not shortened by the preview limit.
- The browser polls `/api/jobs/<id>/live` while the panel is open. The backend reads llama.cpp's streaming response independently, so closing the tab does not interrupt generation. There is still **only one model request at a time**, with no additional model loading.

**Privacy:** the preview can expose generated passages and reasoning about uploaded documents to anyone who can access the web app on your LAN. The app has no authentication. Do not expose it publicly.

**Compatibility:** requires a llama.cpp server supporting streaming OpenAI-compatible Chat Completions (SSE). If your server does not support streaming, this version will not generate document notes; upgrade the server or use the prior non-streaming release.

## Persistent data

```text
.
├── app.py
├── compose.yaml
├── compose.network.example.yaml
├── Dockerfile
├── requirements.txt
├── templates/index.html
├── config/system_prompt.example.txt  # Tracked template
├── config/system_prompt.txt          # Local, Git-ignored
├── .env.example
├── .gitignore
└── data/                  # NOT committed to Git
    ├── jobs.sqlite3
    └── jobs/<job-id>/
        ├── source.pdf
        ├── extracted.json
        ├── chunks.json
        ├── analysis_0000.md
        ├── merge_00_0000.md
        ├── live_preview.json  # Bounded preview of latest model pass
        └── rapport_final.md
```

**Never commit `.env`, `config/system_prompt.txt`, `data/`, uploaded PDFs, SQLite databases, or real analysis reports.** `.gitignore` and `.dockerignore` exclude these common files, but always inspect `git status` before pushing.

The browser can be closed while a job runs. On restart, running jobs are returned to the queue; successful intermediate outputs are reused. A failed job remains failed until retried in the web interface.

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
- **No usable text:** check whether OCR is enabled, language packs match the source, and the PDF is not password-protected.
- **Killed during OCR:** check Docker memory usage and increase `PROCESSOR_MEMORY_LIMIT` carefully if the host has headroom.
- **Unexpected output language:** set `OUTPUT_LANGUAGE` explicitly and retry a new job.
- **Empty reasoning panel:** the model/server did not emit separate reasoning; this is expected for many models.
- **Live output unavailable:** check that llama.cpp supports `stream: true`, then inspect `docker compose logs`.

## Limitations and security

- Tesseract recognizes printed text, **not the meaning of images, charts, diagrams, or complex layout**. PDF extraction may reorder columns and tables. No vision model is included.
- Small language models may hallucinate or omit facts. **Verify all page references and important claims against the original PDF.**
- No web authentication or per-user isolation. **Do not expose this service to the public Internet.** Binding to a trusted LAN interface does not prevent other LAN users from reading documents or jobs.
- One background worker; no priority scheduling, prompt-independent caching or live cancellation. Live output is a bounded preview of a single model pass, not an archive of every generated token.
- Stored jobs may include sensitive data. Protect host filesystem permissions and back up `data/` appropriately.
- Very large or complex PDFs may exceed Docker memory limits even if their uploaded file size is small.

## Development and tests

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

OCR tests require `tesseract` and the English/French language packages on the test machine, or can be run within the built Docker image. A GitHub Actions workflow runs the automated tests on pushes and pull requests.

## License

MIT — see [LICENSE](LICENSE). Contributions and forks are welcome.
