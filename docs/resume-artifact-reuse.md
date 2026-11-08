# Selective reuse during configurable resume

The pipeline first checks the selected configuration's normal retry artifacts.
On a miss in a budget revision, it uses `Artifacts.read_compatible()` to search
up to 32 older persisted revisions of the same job. Compatibility allows only
nondecreasing output budgets and the `warnings`/`customized` metadata fields.
Original configuration fingerprints, directories and version 1 manifests remain
unchanged. Historical files are neither copied nor linked into the new revision.

Reuse requires the original PDF identity, a freshly bound model/template/sampling
identity, a completed manifest entry, exact step dependencies and a SHA-256 hash
of the returned bytes. Incomplete streams are discarded; a new generation starts
from the prepared input and uses the selected revision's output budget.

Merge planning and final-input preparation run again with the current budgets.
An increased output budget can reduce available input context, change groups or
omit prompt evidence. Matching artifact names never substitute for matching
the complete dependencies, including groups and the exact prepared input.
Reliability checks run again on every reused generation before publishing a report.

Each recorded reused step has `artifact_reused`, `artifact_source_revision`,
`artifact_source_config_hash` and `artifact_source_sha256`. For generations,
`output_budget` is the **source generation's** budget;
`requested_output_budget` is this attempt's selected budget. Old token usage
is not charged to this attempt. Attempt configuration references remain pinned
in SQLite. Saving still archives an existing report under `previous_reports`.

The attempt-local verification cache retains bounded history and manifest
metadata, never artifact text. Within historical lookup, the PDF is hashed once
while its file state stays unchanged; the existing PDF check at job startup is
preserved separately. Unchanged manifests are parsed once, with an LRU bounded to 4 MiB of
serialized input in total. File state is checked on subsequent lookups; changed
sources and manifests are reverified. Every artifact returned is read and hashed
again. Normal local retry hits skip historical lookup. Cancellation remains
checked during historical lookup and PDF hashing, and before using a result.

## Deterministic generation-call comparisons

`tests/test_pipeline_resume.py` uses real temporary SQLite/filesystem storage,
the worker and HTTP resume/retry routes, and the real SSE completion validator
with simulated model streams. Eight sections produce two merges and one final
report: **11 fresh generation calls**, at both the original and increased test
budgets. These are call counts, not measurements of inference speed or tokens.

| Resume trigger | Calls in resumed attempt | Calls saved versus fresh |
| --- | ---: | ---: |
| First merge hits output limit | 2 merges + 1 final = 3 | 8 |
| Second merge hits output limit | 1 merge + 1 final = 2 | 9 |
| Fourth section hits output limit | 5 sections + 2 merges + 1 final = 8 | 3 |
| Final hits output limit | 1 final | 10 |
| Report saving fails after completed final | 0 | 11 |

A separate context fixture changes merge groups from four notes to two after
increasing the merge budget. The old completed merge is rejected: the resumed
attempt makes four merges and one final (5 calls), versus 13 calls for a fresh
analysis with that grouping, saving 8 calls. Another fixture changes prepared
final input and proves that an otherwise completed final must be regenerated.

The suite also covers corruption, dependency changes, backend changes, cancellation
during reuse and streaming, successive revisions, restart recovery, normal retries,
attempt limits, and original generation-budget provenance. It asserts generation
counts and proves one history lookup, one PDF read in the artifact layer and one
historical manifest read for the compatible final-failure resume fixture.

## Limits

Lookup remains conservative: artifacts over 16 MiB, manifests over 4 MiB,
unverifiable identities, unsafe links and platforms without safe directory-relative
file opening produce cache misses. Revisions beyond the lookup bound are not
searched, even when they might contain compatible work. Reused text remains in
its source revision, so later attempts verify that source again. Checksums provide
integrity checks within the application's private storage, not authentication
against an actor who can rewrite both content and its manifest.
