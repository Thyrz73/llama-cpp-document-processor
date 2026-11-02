# Application API

The API uses the same origin as the web interface (port 9932 by default). Responses under `/api/` have `Cache-Control: no-store`. Errors contain `error` and, where available, `error_code`. Job IDs are 32 lowercase hexadecimal characters.

There is no authentication or per-user isolation. Browser POSTs from another origin are rejected, but this does not replace access control.

## Routes

| Method and path | Behavior |
| --- | --- |
| `GET /api/health` | Web app liveness only; no model or worker check |
| `GET /api/jobs` | `jobs`: latest 100; `has_active_jobs`: activity across the whole queue |
| `GET /api/jobs/<id>` | Job status, progress, phase, question, filename and public configuration |
| `POST /api/jobs` | Multipart PDF upload; returns 201 with ID, queued status and configuration |
| `GET /api/jobs/<id>/report` | UTF-8 Markdown attachment, `analysis_<first 8 ID characters>.md` |
| `GET /api/jobs/<id>/live` | Latest bounded preview; never starts generation |
| `POST /api/jobs/<id>/retry` | Empty body; requeues a failed/cancelled job with its original settings |
| `POST /api/jobs/<id>/cancel` | Requests cancellation |
| `GET /api/jobs/<id>/metrics` | Attempts, step records and summary counts |
| `GET /api/profiles` | Profiles, environment defaults and cached backend capabilities |
| `POST /api/config/preview` | JSON options; returns preliminary configuration without creating a job |
| `GET /api/diagnostics` | Read-only backend inspection, cached for 30 seconds; no inference |

Missing jobs return 404. Report downloads return 409 unless the job is done, 404 if the file is missing, and 409 if empty. The content type is `text/markdown; charset=utf-8`.

## Creating a job

Multipart fields are `file`, `question` (maximum 1,500 characters) and optional `options` containing a JSON object:

```json
{
  "profile": "low",
  "report_language": "fr",
  "detail_level": "standard",
  "overrides": { "analysis_tokens": 3072 }
}
```

The PDF extension, header and upload size are checked at submission. Readability, encryption and page limits are checked during processing. If the question is omitted or empty, the API uses “Analyze the entire document”; whitespace-only requests are rejected.

Options:

- `profile`: `legacy`, `low`, `medium`, `high`, `ultra`. Omitted profile uses environment budgets; the browser selects Low by default.
- `report_language`: `auto`, `fr`, `en`. Automatic follows the request.
- `detail_level`: `concise`, `standard`, `detailed`. Scales the final budget by 0.8, 1 or 1.5 before explicit overrides.
- `overrides`: `chunk_tokens`, `analysis_tokens`, `merge_tokens`, `final_tokens`, `temperature`, `top_p`, `top_k`, `min_p`, `max_calls`, `step_timeout_seconds`, `thinking_section`, `thinking_merge`, `thinking_final`.

Reasoning policies are `inherit`, `on` or `off`; on/off requires an administrator-configured adapter. Context ceilings, endpoints, credentials and concurrency cannot be set per job. Unsupported options and invalid values return 400.

Preview accepts the options object directly as JSON. Both preview and creation use cached capabilities without probing the backend. The full prompt is checked against context limits before generation.

## Progress, retries and cancellation

Statuses are `queued`, `running`, `done`, `failed`, `cancelled`. `progress_percent` measures workflow progress, not remaining time; done is always shown as 100. `phase_code` and `phase_args` support interface translation.

Retry returns 200 when queued, 409 for an incompatible status or an exhausted attempt limit, and 400 for a nonempty body. It preserves saved budgets and prompts. Jobs predating snapshots get `legacy_recomputed` and current settings when processed. Create a new job to use new settings.

Cancel returns 202 when accepted, 200 if already cancelled and 409 for done/failed jobs. Queued jobs become cancelled immediately. Running jobs keep their status with `cancel_requested=true` until cancellation takes effect. OCR finishes or times out first; remote engine release depends on the backend.

## Live output, metrics and diagnostics

Live output keeps only the latest pass, with bounded content and reasoning. The `reasoning` field is empty unless `?include_reasoning=true` is supplied. Only emitted reasoning is available; it may be incomplete. Disabling live output leaves generation streaming enabled.

Metrics accept `?limit=200&before=<step-id>`: limit is 1–500, steps are newest first, and `next_before` identifies the next page. Responses include up to 100 attempts, queue/execution durations, step reuse/failure status and available backend measurements.

Unknown token/timing values remain null or absent. First-event/content latency includes transport and backend waiting. `length` is a failure; `[DONE]` without an explicit finish reason is accepted as `done_without_reason`. There are no automatic generation retries or continuations. Numeric/page-reference checks count warnings, not semantic correctness.

Diagnostics reads `/v1/models` and optional `/props`. It exposes sanitized observations, not credentials, URLs, raw templates or prompts. RSS is the processor's memory, not the engine's or host's. Private system prompts and saved templates are never included in these API responses.
