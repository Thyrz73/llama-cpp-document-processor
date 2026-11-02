"""Sequential, resumable document processing with explicit artifact dependencies."""

import json
import re
import time

from . import jobs, prompts
from .config import fingerprint
from .model_client import Cancelled, ModelError


def plan_groups(notes, fits, max_group=4):
    """Partition in order, never silently dropping a note that does not fit.

    A singleton may pass through unchanged, but at least one group must reduce
    the note count. The caller enforces a level/call budget as a second guard.
    """
    groups, current = [], []
    for note in notes:
        candidate = current + [note]
        if current and (len(candidate) > max_group or not fits(candidate)):
            groups.append(current)
            current = [note]
        else:
            current = candidate
    if current:
        groups.append(current)
    if not any(len(group) > 1 for group in groups):
        raise ModelError("merge_impossible", "Notes cannot be merged within the context budget. Start a new job with adjusted budgets.")
    return groups


def run(job_id, runtime):
    """Execute through injected application services, allowing isolated regression tests."""
    r = runtime
    directory = r.job_dir(job_id)
    job = r.get_job(job_id)
    config = jobs.load_config(r.db_conn, job_id)
    if config is None:
        # Historic artifacts remain untouched. Their original settings are unknowable.
        config = r.new_snapshot({})
        config["warnings"].append("legacy_recomputed")
        config["source_hash"] = jobs.file_hash(directory / "source.pdf")
        with r.db_conn() as con:
            jobs.save_config(con, job_id, config)
    if config.get("pipeline_version") != 2:
        raise ModelError("pipeline_incompatible", "This job requires a different pipeline version. Start a new analysis.")
    if jobs.file_hash(directory / "source.pdf") != config["source_hash"]:
        raise ModelError("source_changed", "The source PDF changed. Start a new analysis.")
    with r.db_conn() as con:
        attempts = con.execute("SELECT COUNT(*) FROM job_runs WHERE job_id=?", (job_id,)).fetchone()[0]
    if attempts >= config["max_attempts"]:
        raise ModelError("retry_limit", "The retry limit was reached. Start a new analysis.")
    artifacts = jobs.Artifacts(directory, config, r.atomic_json, r.atomic_text)
    recorder = jobs.Recorder(r.db_conn, job_id, job.get("queued_at") or job["created_at"])
    model = r.ModelClient(config)
    model.cancel_check = lambda: r.cancellation_requested(job_id)
    calls = 0
    preview_path = directory / "live_preview.json"
    phase_args = {}

    def phase(code, label, percent=None, **args):
        """Persist stable phase identifiers alongside the historical English label."""
        nonlocal phase_args
        model.check_cancelled()
        phase_args = args
        values = {"stage": label, "phase_code": code, "phase_args": json.dumps(args)}
        if percent is not None:
            values["progress_percent"] = percent
        r.update_job(job_id, **values)
        if r.LIVE_OUTPUT_ENABLED and code != "saving":
            r.atomic_json(preview_path, {"phase": label, "phase_code": code, "phase_args": args,
                                        "content": "", "reasoning": "", "active": False,
                                        "updated_at": jobs.now(), "metrics": {}})

    def saved_step(name, stage, dependencies, compute):
        """Reuse verified text or record the complete outcome of a fresh computation."""
        model.check_cancelled()
        if stage in ("section", "merge", "final"):
            model.current_metrics = {}
            # Pre-fix artifacts may contain Latin-1-decoded UTF-8 even when
            # their file hashes are valid. Recompute on an explicit retry;
            # never try to repair their Unicode strings heuristically.
            dependencies = {**dependencies, "text_transport_version": 1}
        handle = recorder.start(stage, name)
        before_calls, before_ms = model.tokenization_calls, model.tokenization_ms
        before_hits = model.tokenization_cache_hits
        try:
            cached = artifacts.read(name, dependencies)
            if cached is not None:
                recorder.end(handle, "reused", {"artifact_reused": True})
                if r.LIVE_OUTPUT_ENABLED and stage in ("section", "merge", "final"):
                    r.atomic_json(preview_path, {"phase": r.get_job(job_id)["stage"], "phase_code": stage,
                        "phase_args": phase_args, "content": cached[-r.LIVE_PREVIEW_CHARS:],
                        "content_chars": len(cached), "reasoning": "", "reasoning_chars": 0,
                        "active": False, "updated_at": jobs.now(), "metrics": {"artifact_reused": True}})
                return cached
            result = compute()
            model.check_cancelled()
            artifacts.write(name, result, dependencies)
            measurements = dict(model.current_metrics) if stage in ("section", "merge", "final") else {}
            measurements.update(tokenization_calls=model.tokenization_calls - before_calls,
                                tokenization_ms=model.tokenization_ms - before_ms,
                                tokenization_cache_hits=model.tokenization_cache_hits - before_hits)
            recorder.end(handle, "done", measurements)
            return result
        except Exception as exc:
            measurements = dict(model.current_metrics) if stage in ("section", "merge", "final") else {}
            measurements["error_code"] = getattr(exc, "code", "step_failed")
            recorder.end(handle, "cancelled" if isinstance(exc, Cancelled) else "failed", measurements)
            raise

    def generate(stage, instructions, text, budget):
        """Enforce the job call budget and publish only bounded preview channels."""
        nonlocal calls
        if calls >= config["max_calls"]:
            raise ModelError("call_limit", "The job exceeded its generation-call limit.")
        calls += 1
        model.current_stage = stage
        started = time.monotonic()
        started_at = jobs.now()
        model.current_metrics = {}

        def live(content, reasoning, completed):
            """Write a cheap public preview, keeping useful text and reasoning separate."""
            if not r.LIVE_OUTPUT_ENABLED:
                return
            stats = dict(model.current_metrics)
            r.atomic_json(preview_path, {
                "phase": r.get_job(job_id)["stage"], "phase_code": stage, "phase_args": phase_args,
                "content": content[-r.LIVE_PREVIEW_CHARS:], "reasoning": reasoning[-r.LIVE_PREVIEW_CHARS:],
                "content_chars": stats.get("content_chars", len(content)),
                "reasoning_chars": stats.get("reasoning_chars", len(reasoning)),
                "active": not completed, "updated_at": jobs.now(),
                "started_at": started_at,
                "elapsed_seconds": round(time.monotonic() - started, 1), "metrics": stats})

        live("", "", False)
        answer = model.complete(instructions, text, budget, on_update=live)
        # These mechanical checks are warning signals, not a factual-quality score.
        # Persist counts only: the source's numerical values remain private.
        references = set(re.findall(r"\[Page (\d+)\]", text))
        output_references = set(re.findall(r"\[Page (\d+)\]", answer))
        numbers = set(re.findall(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?", text))
        output_numbers = set(re.findall(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?", answer))
        model.current_metrics["quality_checks"] = {
            "source": "mechanical_warning_only", "missing_page_references": len(references - output_references),
            "unseen_page_references": len(output_references - references),
            "missing_numeric_items": len(numbers - output_numbers)}
        return answer

    def combined(notes):
        """Retain stable ordering and delimit notes without adding conversation history."""
        return "\n\n".join(f"## Notes {i + 1}\n{note}" for i, note in enumerate(notes))

    status = "failed"
    try:
        phase("preparing", "Preparing document", 2)
        capabilities = model.inspect()
        recorder.provenance(capabilities)
        # A name is only a weak identity. Unknown fields remain unknown in diagnostics.
        identity = {"model_id_hash": fingerprint(model.model()),
                    "template_hash": capabilities.get("template_hash"),
                    "engine_sampling_hash": fingerprint(capabilities["sampling"]) if capabilities.get("sampling") else None,
                    "version": capabilities.get("version")}
        artifacts.bind_identity(identity)

        def extract():
            """Serialize extraction metadata together with its exact page mapping."""
            def progress(label):
                matched = re.search(r"page (\d+)/(\d+)", label)
                args = {"index": int(matched[1]), "total": int(matched[2])} if matched else {}
                phase("ocr" if label.startswith("OCR") else "extraction", label,
                      2 + round(5 * args.get("index", 0) / max(1, args.get("total", 1))), **args)
            pages, count, missing, ocr = r.extract_pdf(directory / "source.pdf", progress=progress,
                                                      settings=config["extraction"])
            return json.dumps({"pages": pages, "page_count": count, "missing": missing, "ocr_pages": ocr}, ensure_ascii=False)

        extracted = json.loads(saved_step("extracted.json", "extraction",
                               {"source": config["source_hash"], "extraction": config["extraction"]}, extract))
        pages, page_count = extracted["pages"], extracted["page_count"]
        missing, ocr_pages = extracted["missing"], extracted["ocr_pages"]
        r.update_job(job_id, pages=page_count)
        phase("chunking", "Splitting document", 7)
        chunks = json.loads(saved_step("chunks.json", "chunking",
                            {"extracted": fingerprint(extracted), "budget": config["chunk_tokens"], "identity": identity},
                            lambda: json.dumps(r.make_chunks(pages, model, budget=config["chunk_tokens"]), ensure_ascii=False)))
        if not chunks:
            raise ModelError("empty_document", "Document contains no analyzable text.")
        r.update_job(job_id, total_chunks=len(chunks), progress_percent=10)
        notes = []
        for i, chunk in enumerate(chunks):
            phase("section", f"Analyzing section {i + 1}/{len(chunks)}", 10 + round(70 * i / len(chunks)),
                  index=i + 1, total=len(chunks), pages=chunk["pages"])
            instruction = prompts.instruction(config, "section", job["question"], f"Excerpt {i + 1} of {len(chunks)}.")
            notes.append(saved_step(f"analysis_{i:04d}.md", "section",
                         {"chunk": fingerprint(chunk), "instruction": fingerprint(instruction), "identity": identity},
                         lambda: generate("section", instruction, chunk["text"], config["analysis_tokens"])))
            r.update_job(job_id, done_chunks=i + 1, progress_percent=10 + round(70 * (i + 1) / len(chunks)))

        warnings = f"Pages with insufficient extracted/OCR text: {missing}. OCR pages: {ocr_pages}."
        final_instruction = prompts.instruction(config, "final", job["question"], warnings)
        merge_instruction = prompts.instruction(config, "merge", job["question"])
        level = 0
        while True:
            model.current_stage = "final"
            final_fits = model.fits(final_instruction, combined(notes), config["final_tokens"])[0]
            if len(notes) <= 4 and final_fits:
                break
            if len(notes) < 2 or level >= 32:
                raise ModelError("merge_impossible", "The final report cannot fit the configured context.")
            model.current_stage = "merge"
            groups = plan_groups(notes, lambda group: model.fits(
                merge_instruction, combined(group), config["merge_tokens"])[0])
            reduced = []
            for index, group in enumerate(groups):
                if len(group) == 1:
                    reduced.append(group[0])
                    continue
                phase("merge", f"Merging level {level + 1}, group {index + 1}/{len(groups)}",
                      min(94, 81 + level * 4), level=level + 1, index=index + 1, total=len(groups))
                text = combined(group)
                reduced.append(saved_step(f"merge_{level:02d}_{index:04d}.md", "merge",
                               {"notes": fingerprint(group), "instruction": fingerprint(merge_instruction), "identity": identity},
                               lambda: generate("merge", merge_instruction, text, config["merge_tokens"])))
            if len(reduced) >= len(notes):
                raise ModelError("merge_impossible", "The merge plan did not reduce its input.")
            notes, level = reduced, level + 1

        phase("final", "Writing final report", 95)
        final = saved_step("rapport_final.md", "final",
                           {"notes": fingerprint(notes), "instruction": fingerprint(final_instruction), "identity": identity},
                           lambda: generate("final", final_instruction, combined(notes), config["final_tokens"]))
        phase("saving", "Saving report", 99)
        handle = recorder.start("saving", "report")
        try:
            header = prompts.report_header(config, job["filename"], page_count, len(chunks), missing, ocr_pages)
            previous_report = directory / "rapport_final.md"
            if previous_report.is_file():
                archive = directory / "previous_reports"
                archive.mkdir(exist_ok=True)
                r.atomic_text(archive / (jobs.file_hash(previous_report) + ".md"), previous_report.read_text(encoding="utf-8"))
            r.atomic_text(directory / "rapport_final.md", header + final + "\n")
            model.check_cancelled()
            r.update_job(job_id, status="done", stage="Analysis completed", phase_code="done", error="", error_code="")
            recorder.end(handle, "done")
        except Exception:
            recorder.end(handle, "failed", {"error_code": "report_save_failed"})
            raise
        status = "done"
        r.log.info("job_finished job_id=%s pages=%d chunks=%d calls=%d", job_id, page_count, len(chunks), calls)
    except Cancelled:
        status = "cancelled"
        raise
    finally:
        recorder.provenance({**model.capabilities,
            "tokenization_calls": model.tokenization_calls, "tokenization_ms": model.tokenization_ms,
            "tokenization_cache_hits": model.tokenization_cache_hits, "generation_calls": calls})
        recorder.finish(status)
        model.session.close()
