# Updating the existing application safely

This is an in-place application update: `python app.py`, the Compose service, routes, port, source PDF names and report download path remain unchanged. New Python modules and static assets are included in the Docker image. No llama.cpp container, model or engine setting is modified by startup or migration.

## Before deployment

1. Finish or cancel queued/running work. Review historical incomplete jobs: their original parameters cannot be reconstructed reliably.
2. Stop **Document Processor**, keeping llama.cpp unchanged.
3. Back up the entire `data/` directory, `.env`, private `config/system_prompt.txt`, the old application source/image and deployment configuration. A raw copy of a live SQLite/WAL database is not a consistent backup; use a stopped processor or SQLite's backup API.
4. Validate the backup and retain it separately. It contains private documents and prompts.
5. Install the updated source including `document_processor/`, `static/`, `templates/`, `scripts/` and `Dockerfile`. Preserve mounted data and private configuration. The profile registry intentionally lives outside the mounted `config/` directory.
6. Review `.env.example`; new engine capability controls default to disabled/unverified. Existing variables keep their meanings for clients without a selected profile.

Do not execute a deployment just because this file exists. Choose and authorize a deployment window separately.

## Migration behavior

Startup applies additive, idempotent SQLite migrations. Existing job rows and finished timestamps are preserved except for normal recovery of interrupted running jobs. `job_configs`, `job_runs`, `job_steps` and `schema_migrations` store the new state. Private configuration is kept out of the public `jobs` row serializer.

New jobs capture their resolved configuration, source hash, private system prompt, task templates, algorithm version, OCR settings, language and reasoning policies. A retry cannot replace the snapshot. Credentials and endpoint URLs are not stored in it. Prompt changes apply only to new jobs.

Artifact files live under `data/jobs/<id>/artifacts/<configuration-hash>/`. A manifest records content hashes and input dependencies. A completed file without a matching manifest entry is recomputed after an interrupted write. A changed source PDF or known model/template identity refuses unsafe reuse. An advertised model name is not proof of a GGUF binary identity; absent engine metadata remains a reproducibility limitation.

Historical completed reports remain downloadable from the original path. Historical incomplete jobs without snapshots are recomputed under the current captured settings, with a `legacy_recomputed` warning. Their old extraction, chunk and note files are not deleted. If an existing root report is replaced, its previous bytes are retained under `previous_reports/<hash>.md`. This can consume additional disk space; there is no automatic retention cleanup.

Inspect a stopped data directory or backup without starting a worker:

```bash
python scripts/check_jobs.py /path/to/data
```

The command opens SQLite in read-only mode and prints counts, not document contents. Historical jobs without snapshots are reported separately from integrity failures. Tests exercise repeated migrations, old databases, changed inputs, failed steps, corrupt artifacts and immutable prompts.

## After an authorized deployment

Check health, existing job listings and an existing Markdown download before starting an authorized small test PDF. Confirm that profile previews, language/theme choices, metrics and cancellation behave as expected. Verify filesystem permissions and reverse-proxy authentication. If using the optional Docker network overlay, keep including it in Compose commands.

No inference is required merely to inspect old reports or profiles. Diagnostics perform only read requests against the configured model backend. Do not run a full benchmark on the reference 8 GB machine as an installation check.

## Rollback

Stop Document Processor. Back up the post-update data separately so new results are retained. Restore the **matching old application and its pre-update data/configuration backup together**. Do not merge SQLite databases or let old code blindly resume new jobs: its artifact rules and statuses differ. Returning only the code is not a safe reproducibility rollback. New Markdown reports may be retained separately as documents without transplanting their execution state into the old database.

No llama.cpp restart is required for application rollback. Keep both backups until the selected version and downloads are verified. This document provides a procedure; the application does not run these operations automatically.
