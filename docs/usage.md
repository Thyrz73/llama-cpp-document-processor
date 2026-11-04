# Using the interface in detail

The [README](../README.md) covers installation and everyday use. This page details the interface, reports, history and monitoring.

## Analyses and history

Active analyses appear first in a highlighted section; history appears below a separate heading. The list shows the latest 100 jobs; older jobs remain stored.

Saved custom analysis instructions are available in a collapsible block on each job, during processing and in history. Line breaks and the complete submitted text are preserved. Default instructions do not create a custom-prompt block. Older jobs show their saved request with a neutral label when its origin is unknown; server system prompts and task templates stay private.

Finished analyses can be deleted one at a time or selected in bulk from the displayed history. Each deletion asks for confirmation, and active analyses cannot be selected. Deleting removes the job record and its private files; see [history deletion](api.md#history-deletion) for the guarantees and warnings.

The interface initially selects `low`, automatic report language and standard detail. Interface language and theme are saved in the browser; changing them does not translate existing reports. Automatic report language follows the request, not the interface language.

## Reports

Reports use a deterministic descriptive download name such as `Consommation_energetique_des_batiments_2025-2026_rapport.md`, derived from an existing Markdown heading. Generic headings are skipped; the fallback is `<original PDF stem>_rapport.md`. Names use portable ASCII and stay within 100 characters. No model call or PDF reanalysis is needed. The internal `rapport_final.md` path stays unchanged, and older reports remain supported: their names are computed and saved on first view or download, and listing history never reads Markdown contents.

The server-rendered reader supports headings, lists, tables, quotes, links and code, with selectable Unicode text and the saved light/dark theme. Raw HTML is escaped, executable link schemes are rejected and images are disabled. A restrictive Content Security Policy blocks embedded resources. Preview is limited to **1 MiB**; larger files remain downloadable without loading the full file into application memory. Empty reports show an explicit message and can still be downloaded. Only completed final reports are exposed; private configurations and intermediate pipeline files are not published.

## Cancel and retry

**Cancel** stops queued jobs immediately and requests cancellation of running work. OCR finishes its current subprocess first; a remote model may take longer to stop. **Retry** is available for failed or cancelled jobs and keeps their original settings. Create a new analysis to change budgets, language or prompts. If the server refuses a cancellation or a retry, for example once the retry limit is reached, the reason appears above the analyses list.

If a cancelled or timed-out HTTP reader is still closing, the next generation waits for it instead of failing subsequent jobs. This wait remains cancellable and does not consume the next generation's time limit. A response arriving after cancellation is closed before reading its body.

## Live output and refresh

Jobs refresh every six seconds while work is active, with backoff on errors. Live output refreshes every 1.7 seconds while its panel is open. Both pause when the tab is hidden. Live output shows only the current or latest model pass, and reasoning appears only when the backend emits it.

## Logs, metrics and checks

Application events go to stdout/stderr for `docker compose logs -f`: startup, recovery, submissions, extraction/OCR, section/merge/final steps, reuse, cancellation and report access. Events include job IDs, durations measured with a monotonic clock, progress and token usage when the backend supplies it. Logs contain neither PDF text nor full prompts/model responses. Tracebacks retain frame locations while omitting source lines and private exception messages. Streaming updates are never logged, including at DEBUG. Existing on-disk generation diagnostics remain separate. Docker log rotation stays under deployment control.

Metrics report application durations and optional backend usage/timings. Unknown values stay unknown; first-event latency is not engine prefill time. Numeric/page-reference counts are warnings, not factual scores. See [API details](api.md) and [benchmark instructions](engine-tuning.md).

Documentary checks retain physical page/passage provenance separately from model notes. They flag nonexistent pages, some exact-quotation page mismatches and possible entity/delta errors in explicitly structured numeric rows. Source reserves and figures are carried through merges in a bounded evidence block; omitted extracts remain in private `source_evidence.json`. Detailed checks are saved in `quality_checks.json`, and warnings appear in the downloaded report. These checks neither repair numbers automatically nor verify scientific correctness or every citation. All four profiles use the same checks without an additional LLM pass. See the [implementation, limits and validation report](documentary-reliability.md).
