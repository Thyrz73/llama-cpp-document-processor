# Using the interface in detail

The [README](../README.md) covers installation and everyday use. This page details the interface, reports, history and monitoring.

## Analyses and history

Active analyses appear first in a highlighted section; history appears below a separate heading. The list shows the latest 100 jobs; older jobs remain stored.

Saved custom analysis instructions are available in a collapsible block on each job, during processing and in history. Line breaks and the complete submitted text are preserved. Default instructions do not create a custom-prompt block. Older jobs show their saved request with a neutral label when its origin is unknown; server system prompts and task templates stay private.

Finished analyses can be deleted one at a time or selected in bulk from the displayed history. Each deletion asks for confirmation, and active analyses cannot be selected. Deleting removes the job record and its private files; see [history deletion](api.md#history-deletion) for the guarantees and warnings.

Use **Select** to show the history checkboxes and select-all control. Bulk actions appear below the list once an analysis is selected. **Cancel** exits selection mode and clears the selection; the individual trash buttons return. Refreshes and language changes preserve visible selections and keyboard focus. While deletion is pending, selection controls are disabled; failed deletions retain their entries and selection.

The interface initially selects `low`, automatic report language and standard detail. Interface language and theme are saved in the browser; changing them does not translate existing reports. Automatic report language follows the request, not the interface language.

The initial theme is light. An explicit light/dark choice is remembered on both the analysis page and report reader. The retired `system` setting and invalid saved values fall back to light, independently of the operating system's color scheme.

For new analyses, headers, extraction notices and mechanical warnings use a private `effective_report_language` (`fr` or `en`) saved with the configuration. Explicit French/English choices win. With `auto`, a small local heuristic compares distinct common French and English request words; ties or unrecognized requests use English. It examines the request only, not the PDF, and can misclassify short or mixed requests; choose an explicit language when needed. The generation prompt still asks the model to follow the request. Retries reuse the saved choice; older snapshots without this field keep their original rendering (English for `auto`).

## Reports

Reports use a deterministic descriptive download name such as `Consommation_energetique_des_batiments_2025-2026_rapport.md`, derived from an existing Markdown heading. Generic headings are skipped; the fallback is `<original PDF stem>_rapport.md`. Names use portable ASCII and stay within 100 characters. No model call or PDF reanalysis is needed. The internal `rapport_final.md` path stays unchanged, and older reports remain supported: their names are computed and saved on first view or download, and listing history never reads Markdown contents.

The server-rendered reader supports headings, lists, tables, quotes, links and code, with selectable Unicode text and the saved light/dark theme. Raw HTML is escaped, executable link schemes are rejected and images are disabled. A restrictive Content Security Policy blocks embedded resources. Preview is limited to **1 MiB**; larger files remain downloadable without loading the full file into application memory. Empty reports show an explicit message and can still be downloaded. Only completed final reports are exposed; private configurations and intermediate pipeline files are not published.

## Cancel, retry and resume

**Cancel** stops queued jobs immediately and requests cancellation of running work. OCR finishes its current subprocess first; a remote model may take longer to stop.

For a failed or cancelled job, choose **Resume…** to see its saved configuration and failure information. **Retry** keeps its selected effective settings, including budgets from an earlier resume. **Resume with adjusted budgets** lets you increase the analysis, merge and final output token budgets; only modified budgets are submitted. Other settings and prompts stay as saved. Create a new analysis to change them. Older jobs without saved budgets can still use Retry.

Budgets are absolute integer counts (64–65,536), cannot decrease and must fit the saved application and known engine context limits. The dialog, failed-phase guidance and validation errors follow the French/English interface language; changing that language preserves edits and does not change the saved report language. Historical failure metadata can be unavailable, so the dialog then shows general guidance. Current context warnings are shown separately from warnings saved with the analysis.

Output exhaustion may benefit from a larger budget for the affected phase. A context-window limit is different: increasing output budgets does not enlarge the context window and may reduce space for input. The failed phase is shown only when available, and larger budgets do not guarantee success.

Resume keeps completed compatible work and regenerates unfinished or incompatible steps; it never continues partial output. For the deterministic eight-section fixture, a final output-limit recovery needs 1 generation instead of 11 for a fresh run. Changed inputs or merge grouping can reduce those savings; call counts do not measure execution time. See [reuse guarantees, examples and limits](resume-artifact-reuse.md).

Budget revisions and each attempt's configuration survive restarts. Startup requeues interrupted processing and completes pending cancellations; the saved attempt limit still applies. Original settings, attempt history and historical artifacts remain retained, and saving archives an existing report in `previous_reports/`. These retained files use disk space. Reuse caches are bounded, but total PDF, notes and model memory are not covered by that bound; large PDFs can still exceed local memory. Unsafe or oversized historical files and platforms without safe file-opening support may require recomputation.

Polling preserves edits while the dialog is open. If the job changes, close and reopen it to use the latest settings. Escape or **Close** dismisses the dialog when no request is pending. Validation and request errors appear inside the dialog; cancellation errors appear above the analyses list.

If a cancelled or timed-out HTTP reader is still closing, the next generation waits for it instead of failing subsequent jobs. This wait remains cancellable and does not consume the next generation's time limit. A response arriving after cancellation is closed before reading its body.

## Live output and refresh

Jobs refresh every six seconds while work is active, with backoff on errors. Live output refreshes every 1.7 seconds while its panel is open. Both pause when the tab is hidden. Live output shows only the current or latest model pass, and reasoning appears only when the backend emits it.

## Logs, metrics and checks

Application events go to stdout/stderr for `docker compose logs -f`: startup, shutdown, recovery, submissions, extraction/OCR, section/merge/final steps, reuse, cancellation and report access. Events include job IDs, durations measured with a monotonic clock, progress and token usage when the backend supplies it. Logs contain neither PDF text nor full prompts/model responses. Tracebacks retain frame locations while omitting source lines and private exception messages. Streaming updates are never logged, including at DEBUG. Existing on-disk generation diagnostics remain separate. Docker log rotation stays under deployment control.

Shutdown gives the worker up to 5 seconds to stop cooperatively, leaving interrupted analyses resumable. If it cannot stop in time, `worker_shutdown_timeout` is logged. The final checkpoint has a 1-second lock timeout; `sqlite_checkpoint_incomplete` reports busy or partial checkpoints, while `sqlite_checkpoint_failed` identifies a SQLite exception by type. A complete checkpoint is logged at DEBUG. Keep WAL files when present and follow the [backup instructions](migration-v2.md#update); shutdown is not a guarantee that the database file alone is a complete backup.

Metrics report application durations and optional backend usage/timings. Unknown values stay unknown; first-event latency is not engine prefill time. Numeric/page-reference counts are warnings, not factual scores. See [API details](api.md) and [benchmark instructions](engine-tuning.md).

Documentary checks retain physical page/passage provenance separately from model notes. They flag nonexistent pages, some exact-quotation page mismatches and possible entity/delta errors in explicitly structured numeric rows. Source reserves and figures are carried through merges in a bounded evidence block; omitted extracts remain in private `source_evidence.json`. Detailed checks are saved in `quality_checks.json`, and warnings appear in the downloaded report. These checks neither repair numbers automatically nor verify scientific correctness or every citation. All four profiles use the same checks without an additional LLM pass. See the [implementation, limits and validation report](documentary-reliability.md).
