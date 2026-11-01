# Engine diagnostics, optimization and calibration

Profiles change application work, not machine resources. They do not load another model, add a GPU, change llama.cpp slots, resize KV cache or alter threads. All four profiles are experimental and sequential until measured on a verified backend. Report detail is a separate preference.

## Establish a baseline

Record the actual llama.cpp build, GGUF identity/hash, quantization, chat template, context per request/slot, slots, threads, batch/ubatch, KV types and relevant container limits. Use sanitized startup logs and already available diagnostics. Current environment values do not prove what a historic task used.

For the audit's task 1570, the reported 1,841 generated tokens versus the repository's default section budget of 1,000 still requires the historic payload/configuration, stage association and finish reason. The code does send `max_tokens`. Do not explain the difference as a separate extra reasoning allowance without evidence. Sections do not accumulate previous conversation history.

The audit's approximately 50-minute request is a reported observation, not a measurement of this implementation. No speed gain or memory saving is claimed by the profile values.

## Application experiments

Start with fresh jobs and compare one variable at a time:

1. Compact factual notes: preserve facts, numbers/units, uncertainty, contradictions and page references; inspect intermediate notes and merges, not only the final report.
2. Verified stage-specific reasoning. Compare section, merge and final separately. Qwen3 official controls do not prove support by an abliterated derivative. Keep sampling stable during this comparison.
3. Output budgets: detect `length`; do not exchange useful work for silently truncated notes. No automatic continuation is enabled.
4. Chunk size: shorter prompts may increase calls and repeated prefill. Judge whole-document time and quality.
5. Adaptive grouping: inspect depth, costs and lost facts. The implementation partitions deterministically, passes singletons through and refuses non-progressing plans.

Tokenization counts are cached inside a job with bounded hashed keys. Page markers and paragraph/newline boundaries are preserved where possible; oversized fragments still need splitting. No overlap is added without evidence that its cost improves document coverage. OCR does not interpret figures or complex table semantics.

## External engine experiments — separate approval/window

Verify syntax and support against the **installed build**, not only today's documentation:

| Experiment | What to measure and protect |
| --- | --- |
| Generation threads: 1 vs 2 on the reference CPU | Decode throughput, total duration, quality and other load. Four logical threads do not prove four threads are faster. |
| Prompt threads, batch and ubatch | Prefill duration, working buffers, peak memory; vary independently. |
| Engine context: e.g. 16K vs 12K | Actual KV allocation and context capacity. Changing application `CONTEXT_TOKENS` alone does not resize engine memory. |
| KV F16 vs Q8 where supported | Memory, compatible attention kernels, throughput and factual quality. Q4_K_M model weights do not identify the KV type. |
| Flash Attention / GPU offload | Confirm backend/build support and actual offload in logs; measure prefill and decode independently. |
| Slots / concurrency | Keep one local generation initially. Parallel sections require measured headroom and coordinated scheduling/recovery, not merely a larger UI profile. |
| mmap/mlock | Do not promise memory reduction from mlock. Watch host pressure and other services. |

On the reference 8 GB host, never load a second full model concurrently for benchmarking. Pauses normally add time and are not introduced automatically. Consider them only when synchronized measurements establish a recoverable constraint.

Investigate extreme `0.02 token/s` episodes separately from gradual decode slowdown. Align engine timestamps with active swap in/out, major faults, memory pressure, CPU quotas/throttling, frequencies, temperatures and competing work. Allocated swap is not proof of active paging. Processor RSS from the application diagnostic is not model memory or total host RAM.

## Reproducible benchmark client

Prepare a plan, without any network access:

```bash
python scripts/benchmark.py --variants docs/benchmark-variants.example.json --repeat 3
```

Only in an authorized test window, with an authorized PDF and a plain text question file:

```bash
python scripts/benchmark.py --variants docs/benchmark-variants.example.json \
  --execute --url http://127.0.0.1:9932 \
  --pdf /path/to/test.pdf --question-file /path/to/question.txt \
  --repeat 3 --cache-condition unknown --output /path/to/new-results.json
```

The tool creates a new job per trial, alternates variant order, collects all metric pages and prints median/min/max duration for successful trials. It never uses Retry to change options. Output excludes question/PDF/report text. Optional proxy credentials come from `PROCESSOR_BENCH_TOKEN`, not command-line arguments. A timeout or keyboard interruption requests cancellation of the current benchmark job; remote backend cancellation still depends on the server.

Changing the profile changes multiple budgets; comparisons require `--allow-combined` and cannot establish an individual causal effect. Separate cold/warm cache conditions honestly: the tool records the declared condition but does not flush caches or restart the engine. Compare compatible model/build/template observations and report failures as well as successes.

An optional `--checklist` accepts entries with an `id` and required `terms`, as in `quality-checklist.example.json`. These are exact text matches and can miss paraphrases or accept misleading text. They are **not** a factual verifier. Build a human-reviewed checklist from the PDF covering facts, units, uncertainty, contradictions and page references; check every synthesis level. Calibrate permanent defaults only after acceptable quality, repeated measurements and documented resource conditions.

## References to verify against the actual deployment

- [llama.cpp server API and options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Official Qwen3-4B model card](https://huggingface.co/Qwen/Qwen3-4B)
- [llama-bench](https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md)
- [Linux cgroup v2 counters and pressure](https://docs.kernel.org/admin-guide/cgroup-v2.html)

These upstream pages change. They describe potential capabilities, not proof of the installed server configuration.
