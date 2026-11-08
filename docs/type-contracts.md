# Python dictionary contracts

`document_processor/contracts.py` describes the dictionaries already exchanged by the configuration, extraction, model, prompt and pipeline code. These are static `TypedDict` contracts: values remain ordinary dictionaries, lists, strings and numbers. No runtime schema validation, enum conversion, serialization wrapper or database migration is added.

| Contract | Producer and consumers | Requiredness |
| --- | --- | --- |
| `ExtractionSettings` | `Settings.job_defaults()` → config, extraction and pipeline | Extraction controls required; historical `version` optional. |
| `JobConfig` | Deployment defaults / `config.resolve()` → clients and pipeline | Budgets and core controls required. Resolution metadata and flags read with historical fallbacks remain optional. |
| `DiagnosticConfig` | Services → injected diagnostic client | Job controls plus an empty `system_prompt`; no saved templates required. |
| `JobSnapshot` | Snapshot builder / stored JSON → pipeline and prompt construction | Adds required prompt text, templates and prompt version. `source_hash` is optional while building a snapshot and is added by upload. |
| `PublicJobConfig` | `config.public_config()` → history cache and HTTP | Only `fingerprint` required, retaining partial historical records. The existing runtime allowlist still excludes private fields. |
| `PageText`, `Chunk` | Extraction → pipeline | Physical page/text pairs and grouped chunks. `parts` remains optional for historical chunk dictionaries. |
| `PdfExtraction`, `ExtractionResult` | `extract_pdf()` / pipeline JSON artifact | Extraction keeps its tuple return API; `extracted.json` keeps its existing object shape. |
| `AnalysisNote` | Reliability helpers → pipeline merges | Text and provenance required; historical warning codes optional. |
| `ChatMessage`, `CompletionMetrics` | Model adapter → request payload / completion result | Messages retain role/content strings. Metrics are incremental: keys may be absent and unknown measurements may be `None`. |

`ReportLanguage` and `TokenBudgets` are shared field groups. Languages, profiles, reasoning policies, phases and statuses retain their existing strings; in particular, legacy language names remain accepted. Dynamic `thinking_{stage}` keys are unchanged. Numeric annotations do not coerce values: integer engine counts stay integers in JSON, and unknown counts stay null/absent.

`JobConfig` covers both deployment defaults and resolved controls, so it does not assert that optional resolution metadata exists before `config.resolve()`. `JobSnapshot` describes the private snapshot after prompt construction; a job without a snapshot still follows the existing legacy recomputation path. JSON decoding does not fill missing optional keys or remove historical extension fields. Canonical fingerprints, stored snapshots and artifact formats therefore remain unchanged.

## Validation boundaries

Raw options and transport JSON remain dynamic until the existing validation or loading boundary. The small number of concrete `cast` calls marks configuration validation, snapshot extension, fixed public projection and completion metrics; none casts to `Any`. They retain the original objects and do not convert or copy them. Read-only `Mapping` views support dynamic keys without creating dictionaries or changing the runtime validators.

The model client's in-flight metric map and network/capability responses, artifact manifests, and much of the reliability/HTTP implementation remain partly dynamic. PyMuPDF's page interface is isolated behind an `Any` parameter. `Tokenizer` is a small static protocol requiring only `tokenize()`; its optional cancellation hook and existing runtime count checks are preserved. These limits are deliberate; the annotations do not replace runtime input checks.

## Progressive checking

`pyproject.toml` enables per-module strict checks for `contracts`, `config`, `prompts`, `environment` and `settings`, including `disallow_untyped_defs`. Extraction additionally requires annotated definitions, including nested helpers. Model and pipeline boundaries are annotated without requiring every transport/worker/helper function to be typed in this step.

Run the full project MyPy check first, then the scoped strict command in [CONTRIBUTING.md](../CONTRIBUTING.md). The scoped command uses `--follow-imports=silent` to analyze imported dependencies without reporting their additional strict-mode debt; the normal project check still checks those files. The pinned MyPy's strict preset is expressed as explicit per-module options in the configuration to avoid applying it to the entire repository.

`tests/test_contracts.py` covers profile/reasoning keys, JSON round-trips and fingerprints, legacy optional/extra fields, stored/public snapshots, extraction provenance and completion metrics. It also verifies that typed results remain plain dictionaries and completion metrics retain their shared identity with the in-flight map.
