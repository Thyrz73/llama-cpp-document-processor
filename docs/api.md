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
| `GET /api/jobs/<id>/report` | Original UTF-8 Markdown attachment with a descriptive portable filename |
| `GET /api/jobs/<id>/report/view?lang=en` | Server-rendered Markdown reader, optional `lang=en` or `lang=fr`; never starts generation |
| `GET /api/jobs/<id>/live` | Latest bounded preview; never starts generation |
| `POST /api/jobs/<id>/retry` | Empty body; requeues a failed/cancelled job with its original settings |
| `POST /api/jobs/<id>/cancel` | Requests cancellation |
| `POST /api/jobs/<id>/delete` | Deletes one terminal analysis; returns its individual outcome |
| `POST /api/jobs/delete` | JSON `{"ids":["<id>","<id>"]}`; deletes up to 100 analyses with per-ID outcomes |
| `GET /api/jobs/<id>/metrics` | Attempts, step records and summary counts |
| `GET /api/profiles` | Profiles, environment defaults and cached backend capabilities |
| `POST /api/config/preview` | JSON options; returns preliminary configuration without creating a job |
| `GET /api/diagnostics` | Read-only backend inspection, cached for 30 seconds and shared with job start-up inspections; no inference |

Missing jobs return 404. Report access returns 409 unless the job is done and 404 if the file is missing or outside its authorized directory. Empty reports download as empty Markdown and show a message in the reader. Downloads keep `text/markdown; charset=utf-8` and set attachment `filename` and UTF-8 `filename*` parameters. The internal filename remains `rapport_final.md`.

The reader escapes raw HTML, rejects executable link schemes, disables images and sends a restrictive Content Security Policy. Previews over 1 MiB return an HTML page with status 413 and an original download link. The reader also provides localized HTML messages for missing, unreadable and unfinished reports. Download errors retain the JSON API format. No arbitrary file or intermediate artifact route is exposed.

Job responses additionally include `report_name` (null for old jobs until first report access), `report_available` for completed jobs, and `question_custom` (`1` for explicitly customized instructions, `0` for known defaults, null when historical origin is unknown). `question` preserves the original submitted text, including whitespace; the model still receives the same trimmed instructions as before. Configuration snapshots and system prompts remain private.

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

Preview accepts the options object directly as JSON. Both preview and creation use cached capabilities without probing the backend; the cache is refreshed by diagnostics and by each job's start-up inspection. The full prompt is checked against context limits before generation.

## Progress, retries and cancellation

Statuses are `queued`, `running`, `done`, `failed`, `cancelled`. `progress_percent` measures workflow progress, not remaining time; done is always shown as 100. `phase_code` and `phase_args` support interface translation.

Failed jobs store a stable `error_code` and a displayable `error`, for example `encrypted_pdf`, `page_limit`, `no_usable_text`, `ocr_missing`, `deadline` or `identity_changed`. The interface translates known codes. Unexpected internal errors use `analysis_failed` with a generic message; their details are logged without private text.

Retry returns 200 when queued, 409 for an incompatible status or an exhausted attempt limit, and 400 for a nonempty body. It preserves saved budgets and prompts. Jobs predating snapshots get `legacy_recomputed` and current settings when processed. Create a new job to use new settings.

Cancel returns 202 when accepted, 200 if already cancelled and 409 for done/failed jobs. Queued jobs become cancelled immediately. Running jobs keep their status with `cancel_requested=true` until cancellation takes effect. The running job sees an accepted request at its next cooperative check: each phase change, tokenizer call or streamed token, and every 0.2 seconds while it waits for the model. These checks are answered in memory; SQLite is reread at most once per second. OCR finishes or times out first; remote engine release depends on the backend.

After cancellation or a generation deadline, the next generation waits cooperatively for the previous HTTP reader to exit. Cleanup does not cause subsequent jobs to fail, and cancellation remains available while waiting. The next generation's deadline starts after this wait; request/step duration metrics still include elapsed waiting time. If response headers have not arrived, cleanup may wait for the existing network timeout.

## History deletion

The interface confirms individual and bulk removal separately. Selection covers only terminal analyses in the currently displayed list (at most 100), and is reconciled on refresh. Active analyses cannot be selected.

Individual deletion returns `{ "id": "<id>", "deleted": true, "warning_code": null }` on success. Failures return `deleted: false` and an `error_code`: `invalid_job_id` (400), `job_not_found` (404), `delete_active` (409), or `delete_database` (500). Queued/running jobs and terminal jobs whose worker attempt is still finishing are protected. Deletion and retry/worker claims are serialized using SQLite `BEGIN IMMEDIATE`.

Bulk deletion validates the entire list before making changes, deduplicates IDs and processes each analysis independently through the same helper. Invalid, empty or oversized lists return 400 (`invalid_job_ids`), with no deletions. Responses contain `results`, one outcome per unique ID, with HTTP 200 when all records were removed or 207 when any failed. The interface removes only confirmed successes, keeps failed selections and distinguishes record-removal failures from file-cleanup warnings.

Deleting a job cascades to its configuration, attempts and step measurements. The database transaction commits **before** physical cleanup: a database failure never removes files. Only `DATA/jobs/<validated-id>` is eligible for recursive cleanup, including its PDF, reports, previews, artifacts and private logs. No client filename/path or manifest path is used. Shared/global application logs, SQLite files, prompts and other job directories are preserved.

If the private tree is missing, deletion still succeeds. If its root or descendants contain symlinks, hardlinks, mount points, special files, or cannot be verified safely, the entire tree is retained (`warning_code: delete_files_unsafe`). Cleanup requires a symlink-attack-resistant `shutil.rmtree`; unsupported platforms retain files. Filesystem errors return `delete_files_failed`; cleanup may already be partial, but the committed history deletion remains successful. Both warnings are shown in the interface and require administrator inspection of server storage. A process crash between commit and cleanup can likewise leave an orphan directory; there is no automatic orphan purge. These directories are never reused by retry because their job record no longer exists. Storage assumes the application's UUID directories are private and are not deliberately shared by an administrator through external references.

Both endpoints reuse the existing same-origin POST protection and the application's existing access model (no authentication or per-user isolation).

## Live output, metrics and diagnostics

Live output keeps only the latest pass, with bounded content and reasoning. The `reasoning` field is empty unless `?include_reasoning=true` is supplied. Only emitted reasoning is available; it may be incomplete. Disabling live output leaves generation streaming enabled.

Metrics accept `?limit=200&before=<step-id>`: limit is 1–500, steps are newest first, and `next_before` identifies the next page. Responses include up to 100 attempts, queue/execution durations, step reuse/failure status and available backend measurements.

Unknown token/timing values remain null or absent. First-event/content latency includes transport and backend waiting. `length` is a failure; `[DONE]` without an explicit finish reason is accepted as `done_without_reason`. There are no automatic generation retries or continuations. Numeric/page-reference checks count warnings, not semantic correctness.

Step `quality_checks` metrics keep historical raw-string recall counters and add `warnings`, `numeric_checks` and `semantic_validation: false`. Counts do not export entities, expected values or source extracts. Detailed checks, trusted page/section scope and skipped-check status remain in local `artifacts/<configuration-hash>/quality_checks.json`; source evidence is hash-verified in `source_evidence.json`. Neither file is exposed through a new API route. Warnings in the published Markdown do not change job success status. See [documentary reliability](documentary-reliability.md) for supported numeric formats and limits.

Diagnostics reads `/v1/models` and optional `/props`. It exposes sanitized observations, not credentials, URLs, raw templates or prompts. RSS is the processor's memory, not the engine's or host's. Private system prompts and saved templates are never included in these API responses.
