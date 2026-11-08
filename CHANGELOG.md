# Changelog

This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). `document_processor.__version__` is the application version source. The private npm package contains development tools, without an independent application version.

## 2.0.0 — Unreleased

First planned tagged release of the existing v2 application. This entry prepares publication; it creates no tag, release or deployment. Earlier development commits are not retroactively assigned release dates or tags.

### Added

- Reproducible job profiles, immutable snapshots, measured attempts and verified intermediate artifacts ([b8e39d1](https://github.com/Thyrz73/llama-cpp-document-processor/commit/b8e39d1)).
- Documentary provenance and deterministic reliability warnings, without claiming semantic validation ([66ff554](https://github.com/Thyrz73/llama-cpp-document-processor/commit/66ff554)).
- Report reader, UTF-8 downloads, metrics and history controls ([0f819a6](https://github.com/Thyrz73/llama-cpp-document-processor/commit/0f819a6), [e2040a6](https://github.com/Thyrz73/llama-cpp-document-processor/commit/e2040a6)).
- Application factory, injectable services and HTTP blueprints preserving existing routes and legacy entry points ([86e38f5](https://github.com/Thyrz73/llama-cpp-document-processor/commit/86e38f5)).
- In this release preparation: application version in `/api/health`, preserving `status: "ok"`, HTTP 200 and liveness-only behavior; an offline release benchmark; deployment and rollback instructions.

### Changed

- Keep SQLite connections per thread, index job order/status and bound shutdown checkpoint waits ([561139a](https://github.com/Thyrz73/llama-cpp-document-processor/commit/561139a), [cf1531b](https://github.com/Thyrz73/llama-cpp-document-processor/commit/cf1531b), [9554ee5](https://github.com/Thyrz73/llama-cpp-document-processor/commit/9554ee5)).
- Throttle extraction progress persistence and checkpoint live output ([8b394e2](https://github.com/Thyrz73/llama-cpp-document-processor/commit/8b394e2), [0fee694](https://github.com/Thyrz73/llama-cpp-document-processor/commit/0fee694)).
- Cache immutable public settings and stop terminal browser polling ([6a64d23](https://github.com/Thyrz73/llama-cpp-document-processor/commit/6a64d23), [bc35586](https://github.com/Thyrz73/llama-cpp-document-processor/commit/bc35586)).
- Reduce repeated tokenization, reuse compatible audits and track stream lengths incrementally ([d8e3c9a](https://github.com/Thyrz73/llama-cpp-document-processor/commit/d8e3c9a), [f99b333](https://github.com/Thyrz73/llama-cpp-document-processor/commit/f99b333), [843e788](https://github.com/Thyrz73/llama-cpp-document-processor/commit/843e788)).
- Hash-lock dependencies, pin the Docker base and enforce formatting/static checks ([7cd9509](https://github.com/Thyrz73/llama-cpp-document-processor/commit/7cd9509), [a39e552](https://github.com/Thyrz73/llama-cpp-document-processor/commit/a39e552), [17035b1](https://github.com/Thyrz73/llama-cpp-document-processor/commit/17035b1)).
- Separate stream parsing, evidence and report text behind existing interfaces ([79ec64c](https://github.com/Thyrz73/llama-cpp-document-processor/commit/79ec64c), [41ca1c5](https://github.com/Thyrz73/llama-cpp-document-processor/commit/41ca1c5)).

### Fixed

- End-to-end UTF-8 and report-language resolution ([014ec03](https://github.com/Thyrz73/llama-cpp-document-processor/commit/014ec03), [ea16d25](https://github.com/Thyrz73/llama-cpp-document-processor/commit/ea16d25)).
- Cancellation propagates independently of backend failures; interrupted jobs remain recoverable ([8a8b938](https://github.com/Thyrz73/llama-cpp-document-processor/commit/8a8b938), [9554ee5](https://github.com/Thyrz73/llama-cpp-document-processor/commit/9554ee5)).
- Refuse stale backend identity during artifact reuse ([2a455a3](https://github.com/Thyrz73/llama-cpp-document-processor/commit/2a455a3)).
- Restore terminal live output after visibility changes and hide stale legacy previews during queued retries ([6412815](https://github.com/Thyrz73/llama-cpp-document-processor/commit/6412815), [199d3ae](https://github.com/Thyrz73/llama-cpp-document-processor/commit/199d3ae)).
- Prevent diagnostics capability-lock deadlock while preserving injected public overrides and atomic cache publication ([1d09dbb](https://github.com/Thyrz73/llama-cpp-document-processor/commit/1d09dbb), [5c5320e](https://github.com/Thyrz73/llama-cpp-document-processor/commit/5c5320e)).

### Compatibility

- Python 3.12 is the Docker/CI baseline. Node 22 is needed only for frontend development.
- SQLite migrations 1–4 preserve historical rows/files. Downgrades require the old application and its matching coherent backup. Application, schema and artifact/prompt versions are independent.
- Old snapshots and `previous_reports/` remain retained. Legacy jobs may be recomputed; unverifiable source/model/template identity prevents artifact reuse. Retry preserves saved settings and does not automatically continue token-limited output.
- Extraction-process isolation remains deferred after evaluation. OCR cancellation waits for the current operation or timeout. Large PDFs may exceed memory limits despite upload/pixel bounds.
- See [release gates and rollback](docs/releasing.md), [measured validation](docs/release-validation.md) and [saved-job compatibility](docs/migration-v2.md). Publication requires approval and validation of the final commit.
