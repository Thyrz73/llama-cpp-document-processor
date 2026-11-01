# Application API and compatibility

The existing Flask application, port, SQLite file, data volume and report URLs remain in use. No separate V2 service is created. Responses contain public configuration only; the private system prompt and template snapshots are never returned.

## Existing routes

- `GET /api/health`: application liveness only, no model request.
- `GET /api/jobs`: latest 100 jobs and activity across the whole queue. Existing fields remain. New fields include `phase_code`, `phase_args`, `error_code`, `cancel_requested`, `queued_at` and, for new jobs, `configuration`.
- `GET /api/jobs/<id>`: the same public job representation.
- `POST /api/jobs`: multipart `file` and `question` remain supported. Optional `options` contains JSON as shown below. Unknown fields, invalid budgets and unsupported reasoning controls return 400.
- `GET /api/jobs/<id>/live`: bounded useful output, phase, activity and available metrics. Reasoning is omitted by default; `?include_reasoning=true` explicitly requests the retained emitted channel. This privacy change requires old clients displaying reasoning to add the parameter. No generation is started.
- `POST /api/jobs/<id>/retry`: failed or cancelled jobs only; empty body. Reuses the original snapshot. Changing parameters requires a new job. Concurrent/repeated requests return 409 once the job is queued. Historical jobs without snapshots return a `legacy_recomputed` warning. Attempts are bounded by the saved administrative limit.
- `GET /api/jobs/<id>/report`: unchanged Markdown attachment, `analysis_<8-character-id>.md`; 404 for missing files/jobs, 409 for unfinished/empty results. Never starts inference.

```json
{
  "profile": "low",
  "report_language": "fr",
  "detail_level": "standard",
  "overrides": {"analysis_tokens": 1100}
}
```

Omitting `options` uses historical environment defaults. Explicit profiles are `low`, `medium`, `high`, `ultra`. Report language is `auto`, `fr` or `en`; detail is `concise`, `standard` or `detailed`. Advanced overrides allow `chunk_tokens`, `analysis_tokens`, `merge_tokens`, `final_tokens`, `temperature`, `top_p`, `top_k`, `min_p`, `max_calls`, `step_timeout_seconds`, and `thinking_section/merge/final` (`inherit/on/off`). URLs, credentials, context ceilings, engine commands and concurrency are not job options.

## Additional routes

- `GET /api/profiles`: central profile registry, legacy defaults, cached capabilities and reasoning-control availability. No inference or engine probe.
- `POST /api/config/preview`: JSON options; returns preliminary resolved settings and warnings without creating a job. Every complete prompt is checked again immediately before generation. Preview calls use a recent diagnostic cache if available.
- `GET /api/diagnostics`: cached (30 seconds) read-only `/v1/models` and optional `/props` observations. Missing properties remain unknown. Model names are reduced to a basename; URLs, raw templates, slot prompts and arbitrary build metadata are not exposed. Process RSS measures **Document Processor**, not the model container or whole host. No Docker socket is needed.
- `GET /api/jobs/<id>/metrics?limit=200&before=<step-id>`: attempt summaries and step records, newest first. `limit` is 1–500; `next_before` enables pagination. No prompt, PDF or reasoning text appears in measurements.
- `POST /api/jobs/<id>/cancel`: 202 for an accepted request, 200 when already cancelled, 409 for completed/failed jobs. A running job remains `running` with `cancel_requested=true` until a safe cancellation boundary; then it becomes `cancelled`.

API responses use `Cache-Control: no-store`. Errors include a stable code where available and a safe English fallback. The browser translates codes rather than translating generated reports. Cross-origin browser POSTs are rejected; this is not user authentication.

## Metrics and limits

Each attempt has queue waiting and execution duration. Each step records its duration, reuse/failure status and optional model metrics. Usage and timing values carry their source. Missing values are null, never inferred from SSE event count or the Markdown. First event/content latency includes request overhead and backend waiting; it is not equivalent to engine prefill time. `length` is recorded as a limit failure without assuming whether output or context caused it. `[DONE]` without an explicit reason is accepted for compatibility but recorded as `done_without_reason`.

Generated tokens include reasoning only to the extent defined by the engine's usage fields. A useful-token count is derived only from a consistent explicit completion/reasoning breakdown. Mechanical numeric/page-reference checks are advisory counts, not semantic coverage guarantees.

The real generation deadline and cancellation monitor run independently of network token arrival. If an HTTP reader takes time to close, a process-wide gate prevents another local generation until it exits. A remote server may take longer to release its slot. OCR cancellation takes effect after its bounded subprocess finishes or times out. Tokenizer requests retain bounded network timeouts. There are no automatic generation retries or automatic continuations after `length`.

## Deployment boundary

The service remains for a trusted private installation. There are no accounts or per-user ownership checks. Anyone with application access can read its jobs, reports and explicitly requested live reasoning. Use an authenticated reverse proxy and restrict network/filesystem access; do not describe this as an isolated public multi-user service. Engine URL/credentials are administrator-owned environment variables. A user's uploaded PDF or advanced options cannot select arbitrary network targets.
