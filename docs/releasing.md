# Preparing and deploying a release

Version `2.0.0` prepares the first tagged release of the existing v2 application. `document_processor/__init__.py` is the version source; `/api/health` reports it alongside `status`. Keep the changelog **Unreleased** until publication is approved. The preparation commit is not a publication date.

The documented HTTP behavior in [api.md](api.md) is the public contract. Apply [SemVer](https://semver.org/spec/v2.0.0.html) to later releases. SQLite migration numbers, prompt versions and artifact identity are separate compatibility mechanisms.

## Gates before merging and publishing

1. Compare against the current GitHub `develop` and review the full PR diff. At preparation start, `develop` was `afa5404ccc4ffa6d3f77942eb9b6dd7ff20d71ca`; `5c5320e` was 65 commits ahead and zero behind. Recheck if either branch moves; do not merge/rebase just to verify the comparison.
2. Run the locked Python 3.12 / Node 22 checks in [CONTRIBUTING.md](../CONTRIBUTING.md), including coverage. Validate legacy database migration, artifact reuse/identity refusal, cancellation/restart, live output and French/English reports with disposable data and simulated model responses.
3. Require green CI on the **final PR head**, including Compose, Docker build and the constrained runtime smoke. An older green commit validates only that commit. Keep the single CI job until measurements justify duplicated setup or parallel jobs.
4. Test the image on the target Docker host under the default 512 Mio / 0.75 CPU limits, with `OMP_THREAD_LIMIT=1`. Check old reports, mixed PDF/OCR, stop/restart, memory and OOM events. A build or health response alone does not validate memory capacity.
5. Verify a complete backup and exact rollback image. Preserve snapshots, manifests and `previous_reports/`; do not introduce retention or purge behavior.
6. After authorization, push the source branch and open a PR **into `develop`**, including [measured validation](release-validation.md) and outstanding deployment checks. Obtain approval before merging. Any later `main` promotion, release date, `v2.0.0` tag or GitHub release requires an explicit decision on the exact reviewed commit.

The offline container smoke checks legacy migration, retained reports/history, French/English reader, live route, real Tesseract, SIGTERM and backup integrity. It never submits an analysis or requests inference:

```bash
docker compose config --quiet
docker build -t document-processor:release-check .
docker run --rm -i --init --network none \
  --memory 512m --memory-swap 512m --cpus 0.75 --pids-limit 64 \
  -e OMP_THREAD_LIMIT=1 \
  document-processor:release-check python - < tests/smoke_container.py
```

Matching memory and memory-swap disables additional swap; verify enforcement on the target host. [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/).

## Save the current image and complete state

Run from the deployment directory with the original Compose project name and overlays. The commands below use the base file; include the deployed network overlay consistently if applicable. Let the queue drain first: startup resumes interrupted jobs and could otherwise request inference during deployment checks.

Backups contain private documents, credentials and prompts. Keep them outside Git and restrict access:

```bash
umask 077
release_backup="../document-processor-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$release_backup"
processor_container=$(docker compose ps -q document-processor)
test -n "$processor_container"
processor_image=$(docker inspect --format '{{.Image}}' "$processor_container")
rollback_image="document-processor:rollback-$(date +%Y%m%d-%H%M%S)"
docker image tag "$processor_image" "$rollback_image"
docker image save -o "$release_backup/image.tar" "$rollback_image"
printf '%s\n' "$rollback_image" > "$release_backup/image-tag.txt"
cp compose.yaml "$release_backup/compose.yaml"
git rev-parse HEAD > "$release_backup/source-revision.txt"
git archive HEAD > "$release_backup/source.tar"
docker compose stop document-processor
```

Confirm that **every** container/process writing the same SQLite/data directory has actually stopped. Only then make the raw state copy:

```bash
tar -czf "$release_backup/state.tar.gz" .env config/system_prompt.txt data
tar -tzf "$release_backup/state.tar.gz" > "$release_backup/state-files.txt"
```

Save deployment-specific Compose overlays too. Preserve the complete data tree, including `jobs.sqlite3-wal` and `jobs.sqlite3-shm` when present. Do not delete WAL files or assume the main database is complete because shutdown attempted a checkpoint. See [checkpoint limitations](migration-v2.md#update).

Rehearse archive restoration into a separate disposable directory. A SQLite-only live backup can use `Connection.backup()` with explicitly closed source/destination connections; it includes committed WAL state, but does not snapshot PDFs, reports and manifests at the same instant. Stop processing for a complete application backup. [SQLite backup API](https://www.sqlite.org/backup.html).

## Deploy the reviewed revision

Preserve `.env`, the private prompt and all data. From the intended reviewed source:

```bash
docker compose config --quiet
docker compose up -d --build --no-deps document-processor
docker compose ps
docker compose logs --tail=100 document-processor
curl -f http://127.0.0.1:9932/api/health
docker stats --no-stream
```

Adapt address/port as configured. Verify `status`, the intended `version`, old report bytes, French/English views, history/live and migration logs. Reading reports requests no inference. Keep the old image and backups until acceptance.

The image runs as a non-root user. Make `data/` writable by the configured UID/GID; keep the private bind or an authenticated reverse proxy. Do not scale independent workers sharing this data as part of the release.

## Roll back image and matching state together

Stop the new processor and every writer. Archive post-upgrade data/configuration separately before restoring anything. Keep new Markdown reports separately if needed; do not merge SQLite databases, snapshots or manifests across versions.

Set `release_backup` to the verified pre-upgrade backup. Stage its state in a new directory and load the saved image:

```bash
rollback_restore=$(mktemp -d ../document-processor-restore-XXXXXX)
tar -xzf "$release_backup/state.tar.gz" -C "$rollback_restore"
docker image load -i "$release_backup/image.tar"
rollback_image=$(cat "$release_backup/image-tag.txt")
docker image inspect "$rollback_image" > /dev/null
```

After verifying the staged state and retaining the post-upgrade copy, move the current `data/` aside. Restore the **complete** staged `data/`, `.env` and private prompt with their ownership/permissions. Keep WAL/SHM with their matching database. Select the exact saved image through a private override:

```bash
printf 'services:\n  document-processor:\n    image: %s\n' "$rollback_image" \
  > "$release_backup/rollback.yaml"
docker compose --project-directory "$PWD" \
  -f "$release_backup/compose.yaml" -f "$release_backup/rollback.yaml" config --quiet
docker compose --project-directory "$PWD" \
  -f "$release_backup/compose.yaml" -f "$release_backup/rollback.yaml" \
  up -d --no-build --no-deps document-processor
```

Preserve the original project name, and include any backed-up network overlay before `rollback.yaml`. `--project-directory` keeps relative data/config mounts at the deployment location; `--no-build` prevents replacing the saved image with newer source. Verify logs, health, restored history and report bytes. Older health responses may lack `version`; compare the saved image identity instead. llama.cpp needs no restart.

There is no down-migration. Older numbered-migration code refuses newer schemas. Restore matching state even if schema numbers coincide: snapshot/artifact semantics can still differ. Retain both backups until rollback is accepted.

## Before continuation after token limits

- Preserve original immutable snapshots and source/model/template/sampling identity. A changed budget must not silently make old artifacts compatible.
- Distinguish verified completed steps from partial streams ending in `length`, timeout, disconnection or cancellation. Partial preview text is not a completed reusable artifact.
- Define context reconstruction, provenance, token/call/attempt limits and restart-safe continuation semantics before generating more text. Avoid duplicate appends; preserve citations, source scope, language and metrics.
- Keep continuation opt-in and retain `previous_reports/` and original artifacts on failure/refusal. Validate with offline responses first; real model generation requires separate approval.
- The deferred extraction-process work does not remove OCR cancellation latency or native-reader memory risks.
