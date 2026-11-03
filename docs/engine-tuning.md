# Performance, reasoning and benchmarks

Profiles change token budgets, not machine resources. They do not change the model, threads, GPU, slots or KV cache. All profiles run sequentially and remain experimental; larger budgets do not promise better quality or shorter runs.

## Understand the measurements

The app records queue waiting, step durations, tokenization calls and optional backend usage/timings. Diagnostics shows sanitized backend properties and processor RSS. It cannot measure model memory, host swap activity or the exact GGUF identity.

First-event latency includes network and backend waiting; it is not engine prefill time. Reasoning tokens are counted only when the backend supplies a usable breakdown. Numeric/page-reference checks are warnings, not factual-quality scores.

Record the deployed build, model/hash, quantization, template, context per slot and resource limits when comparing runs. An advertised model name or current environment does not prove what an older job used.

## Application settings

The browser initially selects Low: section input 3000, analysis output 3072, merge output 2048 and final output 3072 tokens. See [all profiles](../README.md#configuration). API jobs without a profile use environment budgets instead.

Change one setting at a time, using fresh jobs:

- **Chunk size:** smaller sections shorten input but may add calls and repeated prompt processing.
- **Output budgets:** preserve essential evidence; output ending with `length` fails rather than being accepted as a complete note.
- **Report detail:** changes the final budget independently of the profile.
- **Reasoning:** compare each stage separately and keep sampling stable.

The pipeline caches token counts within an attempt, merges groups of up to four notes when needed and passes singleton groups through. It rejects merge plans that cannot reduce the notes. Each request is independent; it does not accumulate a chat history.

Quality review should cover the notes, merges and final report: figures/units, assumptions, limits, page references, causal wording and actual contradictions. Synthesis only receives notes. Better prompts do not remove the need to check the PDF.

## Reasoning controls

Set `LLAMA_THINKING_ADAPTER` only after verifying the installed model and template:

| Adapter | Behavior |
| --- | --- |
| `unsupported` | Default; only `inherit` is allowed |
| `chat_template` | Sends `chat_template_kwargs.enable_thinking` for on/off |
| `prompt` | Adds `/think` or `/no_think` for on/off |

`LLAMA_INITIAL_THINKING=true` is only for templates that open a reasoning block without emitting its opening tag. Hiding the reasoning panel does not disable generation.

`LLAMA_USAGE_SUPPORTED` requests streamed usage; `LLAMA_APPLY_TEMPLATE_SUPPORTED` enables rendered-template sizing. Leave both false until backend support is verified. Otherwise sizing uses tokenization with estimated template headroom. A Qwen derivative's name does not establish support for a particular control.

## Benchmark script

Prepare an offline plan; this makes no network requests:

```bash
python scripts/benchmark.py --variants docs/benchmark-variants.example.json --repeat 3
```

To run it against your instance with a test PDF and UTF-8 question file:

```bash
python scripts/benchmark.py --variants docs/benchmark-variants.example.json \
  --execute --url http://127.0.0.1:9932 \
  --pdf /path/to/test.pdf --question-file /path/to/question.txt \
  --repeat 3 --cache-condition unknown --output /path/to/new-results.json
```

This creates a new job per trial, alternates variant order and collects paginated metrics. Choose a new output path; the script refuses an existing one. It prints median/min/max execution duration for successful trials, excluding queue waiting. Timeout or keyboard interruption requests cancellation of the current benchmark job.

Profile comparisons require `--allow-combined` because several budgets change together. `--cache-condition` records your declared condition; it does not flush caches. Optional reverse-proxy bearer credentials come from `PROCESSOR_BENCH_TOKEN`. Exported results exclude the PDF, question and report text.

An optional `--checklist docs/quality-checklist.example.json` checks exact report substrings. Adapt the example to the PDF. Matches can accept misleading statements or miss correct paraphrases, so review factual accuracy separately.

## External engine settings

Change llama.cpp separately from the app. Verify supported options in the installed build and measure one change at a time: threads, batch sizes, context, KV format or GPU offload. Application `CONTEXT_TOKENS` is a ceiling; it does not resize the engine's KV cache.

For very slow generation, compare engine logs with CPU load, container quotas, memory pressure and active swap in/out. Allocated swap alone does not prove active paging. On a constrained host, avoid loading a second model just to benchmark.

Upstream references: [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [llama-bench](https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md), [Qwen3-4B model card](https://huggingface.co/Qwen/Qwen3-4B). These do not establish the capabilities of your installed build.
