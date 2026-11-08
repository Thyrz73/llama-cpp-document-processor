# Artifact growth and retention

There is no automatic purge. This note proposes a future opt-in policy; it adds no setting, scheduled task or deletion code. The default remains to retain job data until explicit history deletion.

## Current growth

Each job retains its PDF, immutable configuration, published report and bounded `live_preview.json`. Preview checkpoints replace one file rather than accumulating versions. SQLite attempts and step metrics accumulate with retries.

`artifacts/<configuration-hash>/` contains extracted text, chunks, section notes, merge levels, final output, evidence, checks and a hash-verified manifest. Recomputing a named artifact replaces it; additional sections/merge layouts and legacy configuration directories can leave older paths behind. Unused artifacts are not swept.

Before replacing a published report, the pipeline archives it as `previous_reports/<sha256>.md`, named from the existing report's file hash. Identical report contents reuse the same archive path; distinct contents add files. Storage therefore grows with source size, distinct reports, artifact layouts and attempts, rather than just the number of visible history rows.

## Proposed retention, disabled by default

A future policy could inventory bytes, age and status without modifying files, then present an explicit per-job archive plan. Its first candidates should be superseded reports from opted-in `done` jobs older than an administrator-selected horizon, with count/byte caps and a verified restore location. No automatic age threshold is enabled here.

Keep the current report, source PDF, configuration, manifest and verified intermediate work together. Exclude queued/running jobs and unfinished attempts. Retain failed, cancelled and token-limited jobs by default, even after retry exhaustion: their artifacts may be essential to compatible retries or future recovery tooling. Current snapshots are immutable; changing token budgets currently requires a new analysis. Terminal status or file age alone does not make intermediate artifacts disposable.

## Existing explicit deletion

History deletion commits SQLite removal before cleaning the job's private UUID directory. It refuses active jobs and unfinished attempts, and retains unsafe/shared filesystem paths. A crash or cleanup warning can leave an orphan directory; there is no automatic orphan purge. Deleting SQLite rows may leave reusable pages rather than shrink the database file.

Any future retention implementation must preserve these ownership checks and coherent backup requirements. See [history deletion](api.md#history-deletion) and [data, updates and backups](../README.md#data-updates-and-backups).
