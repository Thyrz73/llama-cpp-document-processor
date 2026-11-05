# Updates, saved jobs and rollback

Updating Document Processor does not restart or reconfigure llama.cpp. Keep the existing `data/`, `.env` and private `config/system_prompt.txt`.

## Update

Wait for the queue to empty, then stop the processor and back up its state:

```bash
docker compose stop document-processor
umask 077
tar -czf "../document-processor-backup-$(date +%Y%m%d-%H%M%S).tar.gz" \
  .env config/system_prompt.txt data
```

Keep the old source revision or image and Compose files for rollback. Confirm that all processor instances and other writers have actually stopped before making this raw copy. Preserve the complete `data/` directory, including any `jobs.sqlite3-wal` and `jobs.sqlite3-shm` files. Do not delete these files manually or assume that `jobs.sqlite3` alone is complete merely because SIGTERM was sent.

Shutdown waits up to 5 seconds for the worker, then attempts one checkpoint with a 1-second lock timeout. `sqlite_checkpoint_incomplete` reports busy/incomplete outcomes and frame counts; `sqlite_checkpoint_failed` reports SQLite exceptions without their private messages. `worker_shutdown_timeout` means an operation did not cooperate within the worker wait. In these cases, keep the WAL: SQLite recovers committed changes at the next open. `synchronous=NORMAL` still permits loss of recent commits on power failure; these shutdown changes do not strengthen that durability guarantee.

For a coherent database-only backup while SQLite is active, use its [online backup mechanism](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup), such as `source.backup(target)` with explicitly closed source and destination connections. It includes committed WAL data. This does not atomically snapshot PDFs, reports and manifests alongside the database; stop processing for a complete application backup. A raw copy of live DB/WAL files is not a substitute for this mechanism. See also [SQLite checkpoint results](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint) and [WAL persistence](https://www.sqlite.org/wal.html#the_wal_file).

Update the source from the intended branch, then:

```bash
docker compose config --quiet
docker compose up -d --build --no-deps document-processor
docker compose ps
docker compose logs --tail=100 document-processor
curl -f http://127.0.0.1:9932/api/health
```

If using the network overlay, include `-f compose.yaml -f compose.network.example.yaml` in each command. Adapt the health URL to your bind address and port.

Check an existing report download, profile settings and a new small PDF. No model call is needed to view old reports or profiles.

## What happens to existing jobs

Startup adds missing SQLite columns/tables without deleting old results. Interrupted running jobs return to the queue; pending cancellations become cancelled. Failed/cancelled jobs wait for Retry.

New jobs save resolved options, OCR settings, source hash, system prompt and task templates in a private snapshot. Retries keep it. **Changing prompts, profiles or environment values applies to new analyses.** Credentials and the backend URL remain runtime settings.

Work files live under `data/jobs/<id>/artifacts/<configuration-hash>/`. A manifest checks dependencies and file hashes before reuse. Missing, incomplete or damaged steps are recomputed. Changed source PDFs or detected model/template identity changes prevent reuse; the advertised model name alone cannot identify the exact GGUF file.

Old completed reports remain downloadable. Incomplete jobs without snapshots get current settings and a `legacy_recomputed` warning; original files remain. The UTF-8 correction also recomputes generated steps saved before that correction on retry, while retaining compatible extraction. See [UTF-8 handling](utf8-correction.md).

A replaced published report is archived under `previous_reports/<hash>.md`. There is no automatic cleanup.

To inspect a stopped data directory or backup without starting a worker:

```bash
python scripts/check_jobs.py /path/to/data
```

This reads snapshots, source hashes and artifact manifests. It prints counts, not document text. Jobs without snapshots are counted separately; integrity failures produce a nonzero exit code.

## Prompts

The shipped task templates now distinguish causal claims, hypotheses, forecast gaps and missing information. Section analysis checks citations against source markers; later stages preserve the evidence in the notes.

Updating the code provides these templates to new jobs. The private `config/system_prompt.txt` is preserved: compare it with the updated example and merge useful wording manually. A completed job cannot be retried to apply new prompts; upload the PDF as a new analysis.

## Rollback

Stop the processor and save post-update data separately. Restore the old application **and its matching pre-update data/configuration backup together**. Do not merge SQLite databases or let old code resume newer execution state. Versions with numbered schema migrations refuse to start on a database upgraded by a newer version (`Database error: …`).

Recent Markdown reports can be retained separately. Keep both backups until the chosen version and downloads are verified. llama.cpp needs no restart.
