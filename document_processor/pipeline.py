"""Analyze one job: extract its PDF, write notes per section, merge them, save the report.

JobRun.execute() runs these steps in order:

1. prepare  check the llama.cpp backend and bind its identity to the artifacts;
2. extract  read the PDF text, with OCR when needed (extraction.py);
3. split    group the pages into sections that fit the section token budget;
4. analyze  ask the model for factual notes on each section;
5. merge    consolidate notes, level by level, until they fit the final prompt;
6. write    ask the model for the final Markdown report;
7. save     publish the report and mark the job done.

Steps 2 to 6 save their results as verified artifacts (artifacts.py), so a
retry reuses finished work instead of calling the model again. Mechanical
checks (reliability.py) run on every generated text, including reused ones.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import extraction, prompts, reliability
from .artifacts import Artifacts
from .config import fingerprint
from .errors import Cancelled, ProcessingError
from .files import atomic_json, atomic_text, file_hash
from .jobs import PREVIEW_FILE, REPORT_FILE, SOURCE_FILE, JobStore, now
from .model_client import ModelClient
from .observability import event, log
from .reports import report_filename

# Stages that call the model. Their artifacts also depend on the text transport
# and reliability versions, so fixing either forces a fresh generation.
GENERATION_STAGES = ("section", "merge", "final")
# Name of the final report artifact; reliability.report_warnings() reads its checks.
FINAL_ARTIFACT = "rapport_final.md"
# A merge consolidates at most this many neighbouring notes.
MERGE_GROUP_SIZE = 4
# The final prompt receives at most this many notes.
MAX_FINAL_NOTES = 4
# Every merge level must reduce the number of notes, so this limit is only a
# safety net: 32 levels of groups of 4 already exceed any realistic PDF.
MAX_MERGE_LEVELS = 32
# Any number in raw text, for the historical "missing numeric items" counter.
RAW_NUMBER = re.compile(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class Runtime:
    """Everything the pipeline needs from the application.

    app.analyze_job() builds one for each run from the deployment settings;
    tests can build their own to run the pipeline without the web app.
    """

    store: JobStore
    # Creates a configuration snapshot for jobs saved before snapshots existed.
    new_snapshot: Callable[[dict], dict]
    # Returns a client for the deployment's llama.cpp server, bound to a job configuration.
    model_client: Callable[[dict], ModelClient]
    # Tells whether the job's cancellation was requested; called very often.
    cancellation_requested: Callable[[str], bool]
    # Whether to write live_preview.json, and how many characters each channel keeps.
    live_output: bool
    preview_chars: int


def plan_groups(notes, fits, max_group=MERGE_GROUP_SIZE):
    """Partition in order, never silently dropping a note that does not fit.

    A singleton may pass through unchanged, but at least one group must reduce
    the note count. The caller enforces a level/call budget as a second guard.
    """
    groups: list[list[Any]] = []
    current: list[Any] = []
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
        raise ProcessingError("merge_impossible", "Notes cannot be merged within the context budget. Start a new job with adjusted budgets.")
    return groups


def run(job_id, runtime):
    """Analyze job_id with the services provided by runtime (see Runtime)."""
    JobRun(job_id, runtime).execute()


class JobRun:
    """One attempt to analyze one job."""

    def __init__(self, job_id, runtime):
        """Load the job and refuse runs that cannot succeed before recording an attempt."""
        self.job_id = job_id
        self.runtime = runtime
        self.store = runtime.store
        self.directory = self.store.job_dir(job_id)
        self.job = self.store.get_job(job_id)
        self.question = self.job["question"].strip()
        self.config = self._load_config()
        if self.config.get("pipeline_version") != 2:
            raise ProcessingError("pipeline_incompatible", "This job requires a different pipeline version. Start a new analysis.")
        if file_hash(self.directory / SOURCE_FILE) != self.config["source_hash"]:
            raise ProcessingError("source_changed", "The source PDF changed. Start a new analysis.")
        if self.store.attempt_count(job_id) >= self.config["max_attempts"]:
            raise ProcessingError("retry_limit", "The retry limit was reached. Start a new analysis.")
        self.artifacts = Artifacts(self.directory, self.config)
        # Create the client before recording the attempt, so a failure here
        # cannot leave an attempt that looks active forever.
        self.model = runtime.model_client(self.config)
        self.model.cancel_check = lambda: runtime.cancellation_requested(job_id)
        self.recorder = self.store.start_attempt(job_id, self.job.get("queued_at") or self.job["created_at"])
        self.preview_path = self.directory / PREVIEW_FILE
        self.calls = 0
        self.last_percent: int | None = None
        self.phase_args: dict[str, Any] = {}
        # Last stage label written by _phase(); previews reuse it without a database read.
        self.phase_label = self.job["stage"]
        self.quality: dict[str, Any] = {"version": reliability.VERSION, "steps": {}}
        self.identity: dict[str, Any] | None = None  # set by _prepare()
        self.evidence: dict[str, Any] | None = None  # set by _extract()

    def execute(self):
        """Run every step, then record how the attempt ended and close the client."""
        status = "failed"
        try:
            self._prepare()
            extracted = self._extract()
            chunks = self._split(extracted)
            notes = self._analyze_sections(chunks)
            final_instruction = prompts.instruction(
                self.config, "final", self.question,
                f"Pages with insufficient extracted/OCR text: {extracted['missing']}. "
                f"OCR pages: {extracted['ocr_pages']}.")
            notes, final_text, evidence_limit = self._merge_until_final_fits(notes, final_instruction)
            final = self._write_final(notes, final_text, evidence_limit, final_instruction, len(chunks))
            self._save_report(final, extracted, len(chunks))
            status = "done"
            log.info("job_finished job_id=%s pages=%d chunks=%d calls=%d",
                     self.job_id, extracted["page_count"], len(chunks), self.calls)
        except Cancelled:
            status = "cancelled"
            raise
        finally:
            model = self.model
            self.recorder.provenance({**model.capabilities,
                "tokenization_calls": model.tokenization_calls, "tokenization_ms": model.tokenization_ms,
                "tokenization_cache_hits": model.tokenization_cache_hits, "generation_calls": self.calls})
            self.recorder.finish(status)
            model.session.close()

    # Steps --------------------------------------------------------------------

    def _load_config(self):
        """Return the job's snapshot, creating one with current settings for older jobs."""
        config = self.store.load_config(self.job_id)
        if config is None:
            # Historic artifacts remain untouched. Their original settings are unknowable.
            config = self.runtime.new_snapshot({})
            config["warnings"].append("legacy_recomputed")
            config["source_hash"] = file_hash(self.directory / SOURCE_FILE)
            self.store.save_config(self.job_id, config)
        return config

    def _prepare(self):
        """Inspect the backend and refuse to reuse notes written by another model."""
        self._phase("preparing", "Preparing document", 2)
        capabilities = self.model.inspect()
        event("backend_checked", job_id=self.job_id, status=capabilities.get("status", "unknown"))
        self.recorder.provenance(capabilities)
        # A name is only a weak identity. Unknown fields remain unknown in diagnostics.
        self.identity = {"model_id_hash": fingerprint(self.model.model()),
                         "template_hash": capabilities.get("template_hash"),
                         "engine_sampling_hash": fingerprint(capabilities["sampling"]) if capabilities.get("sampling") else None,
                         "version": capabilities.get("version")}
        self.artifacts.bind_identity(self.identity)

    def _extract(self):
        """Read the PDF text (or reuse it) and build the source evidence ledger."""
        def extract():
            pages, count, missing, ocr = extraction.extract_pdf(
                self.directory / SOURCE_FILE, self.config["extraction"], progress=self._extraction_progress)
            return json.dumps({"pages": pages, "page_count": count, "missing": missing, "ocr_pages": ocr},
                              ensure_ascii=False)

        extracted = json.loads(self._step("extracted.json", "extraction",
                                          {"source": self.config["source_hash"], "extraction": self.config["extraction"]},
                                          extract))
        evidence_dependencies = {"extraction": fingerprint(extracted), "version": reliability.VERSION}
        evidence_json = self.artifacts.read("source_evidence.json", evidence_dependencies)
        if evidence_json is None:
            evidence = reliability.build_evidence(extracted["pages"], extracted["page_count"],
                                                  extracted["missing"], extracted["ocr_pages"])
            evidence_json = json.dumps(evidence, ensure_ascii=False)
            self.artifacts.write("source_evidence.json", evidence_json, evidence_dependencies)
        self.evidence = json.loads(evidence_json)
        self.store.update_job(self.job_id, pages=extracted["page_count"])
        return extracted

    def _extraction_progress(self, label):
        """Turn extraction labels such as "OCR recognition: page 3/10" into phase updates."""
        matched = re.search(r"page (\d+)/(\d+)", label)
        args = {"index": int(matched[1]), "total": int(matched[2])} if matched else {}
        self._phase("ocr" if label.startswith("OCR") else "extraction", label,
                    2 + round(5 * args.get("index", 0) / max(1, args.get("total", 1))), **args)

    def _split(self, extracted):
        """Group the pages into sections that fit the section token budget."""
        self._phase("chunking", "Splitting document", 7)
        budget = self.config["chunk_tokens"]
        chunks = json.loads(self._step(
            "chunks.json", "chunking",
            {"extracted": fingerprint(extracted), "budget": budget, "identity": self.identity, "chunk_format_version": 2},
            lambda: json.dumps(extraction.make_chunks(extracted["pages"], self.model, budget), ensure_ascii=False)))
        if not chunks:
            raise ProcessingError("empty_document", "Document contains no analyzable text.")
        self.store.update_job(self.job_id, total_chunks=len(chunks), progress_percent=10)
        event("sections_ready", job_id=self.job_id, total_chunks=len(chunks))
        return chunks

    def _analyze_sections(self, chunks):
        """Ask the model for factual notes on each section, in document order."""
        notes = []
        total = len(chunks)
        for index, chunk in enumerate(chunks):
            self._phase("section", f"Analyzing section {index + 1}/{total}", 10 + round(70 * index / total),
                        index=index + 1, total=total, pages=chunk["pages"])
            instruction = prompts.instruction(self.config, "section", self.question,
                f"Excerpt {index + 1} of {total}. Physical PDF pages: {chunk['pages']}. "
                "Page references inside source prose are not extraction metadata.")
            name = f"analysis_{index:04d}.md"
            answer = self._step(
                name, "section",
                {"chunk": fingerprint(chunk), "instruction": fingerprint(instruction), "identity": self.identity},
                lambda: self._generate("section", instruction, chunk["text"], self.config["analysis_tokens"],
                                       chunk["pages"]))
            check = self._check(name, answer, chunk["pages"], [index])
            notes.append(reliability.note(answer, chunk["pages"], [index],
                                          (issue["code"] for issue in check["issues"])))
            self.store.update_job(self.job_id, done_chunks=index + 1,
                                  progress_percent=10 + round(70 * (index + 1) / total))
        return notes

    def _merge_until_final_fits(self, notes, final_instruction):
        """Merge notes level by level until the final prompt fits them.

        Return (notes, final_text, final_evidence_limit). The loop ends as soon
        as at most MAX_FINAL_NOTES notes fit the final prompt. It cannot run
        forever: every level must reduce the number of notes (see
        _merge_level) and MAX_MERGE_LEVELS bounds the number of levels.
        """
        merge_instruction = prompts.instruction(self.config, "merge", self.question)
        level = 0
        while True:
            self.model.current_stage = "final"
            final_text, final_fits, final_evidence_limit = reliability.prepare_input(
                notes, self.evidence,
                lambda text: self.model.fits(final_instruction, text, self.config["final_tokens"])[0])
            if len(notes) <= MAX_FINAL_NOTES and final_fits:
                return notes, final_text, final_evidence_limit
            if len(notes) < 2 or level >= MAX_MERGE_LEVELS:
                raise ProcessingError("merge_impossible", "The final report cannot fit the configured context.")
            notes = self._merge_level(notes, level, merge_instruction)
            level += 1

    def _merge_level(self, notes, level, merge_instruction):
        """Consolidate neighbouring notes once; return the shorter list of notes."""
        self.model.current_stage = "merge"
        evidence_limits = {}

        def group_fits(group):
            """Tell plan_groups whether a group fits the merge prompt; remember its evidence limit."""
            _, fits, limit = reliability.prepare_input(
                group, self.evidence,
                lambda text: self.model.fits(merge_instruction, text, self.config["merge_tokens"])[0])
            evidence_limits[fingerprint(group)] = limit
            return fits

        groups = plan_groups(notes, group_fits)
        reduced = []
        for index, group in enumerate(groups):
            if len(group) == 1:
                reduced.append(group[0])
                continue
            self._phase("merge", f"Merging level {level + 1}, group {index + 1}/{len(groups)}",
                        min(94, 81 + level * 4), level=level + 1, index=index + 1, total=len(groups))
            evidence_limit = evidence_limits[fingerprint(group)]
            text = reliability.combined(group, self.evidence, evidence_limit)
            name = f"merge_{level:02d}_{index:04d}.md"
            source_pages = sorted({page for item in group for page in item["pages"]})
            answer = self._step(
                name, "merge",
                {"notes": fingerprint(group), "input": fingerprint(text),
                 "instruction": fingerprint(merge_instruction), "identity": self.identity},
                lambda: self._generate("merge", merge_instruction, text, self.config["merge_tokens"], source_pages))
            merged = reliability.merge_note(answer, group)
            check = self._check(name, answer, merged["pages"], merged["sections"], evidence_limit)
            merged["warning_codes"] = sorted(set(merged["warning_codes"]) | {issue["code"] for issue in check["issues"]})
            reduced.append(merged)
        if len(reduced) >= len(notes):
            raise ProcessingError("merge_impossible", "The merge plan did not reduce its input.")
        return reduced

    def _write_final(self, notes, final_text, evidence_limit, final_instruction, chunk_count):
        """Ask the model for the final Markdown report (or reuse it) and check it."""
        self._phase("final", "Writing final report", 95)
        source_pages = sorted({page for item in notes for page in item["pages"]})
        final = self._step(
            FINAL_ARTIFACT, "final",
            {"notes": fingerprint(notes), "input": fingerprint(final_text),
             "instruction": fingerprint(final_instruction), "identity": self.identity},
            lambda: self._generate("final", final_instruction, final_text, self.config["final_tokens"], source_pages))
        self._check(FINAL_ARTIFACT, final, source_pages, range(chunk_count), evidence_limit)
        return final

    def _save_report(self, final, extracted, chunk_count):
        """Publish the report with its header and warnings, then mark the job done.

        A retry archives the previous report first. Marking the job done raises
        Cancelled instead if a cancellation arrived in the meantime.
        """
        self._phase("saving", "Saving report", 99)
        handle = self.recorder.start("saving", "report")
        try:
            header = prompts.report_header(self.config, self.job["filename"], extracted["page_count"], chunk_count,
                                           extracted["missing"], extracted["ocr_pages"])
            report_path = self.directory / REPORT_FILE
            if report_path.is_file():
                archive = self.directory / "previous_reports"
                archive.mkdir(exist_ok=True)
                atomic_text(archive / (file_hash(report_path) + ".md"), report_path.read_text(encoding="utf-8"))
            report_text = header + final + "\n" + reliability.report_warnings(self.quality, self.config["report_language"])
            atomic_text(report_path, report_text)
            name = report_filename(final, self.job["filename"])
            self.model.check_cancelled()
            self.store.update_job(self.job_id, status="done", stage="Analysis completed", phase_code="done", error="",
                                  error_code="", report_name=name)
            event("report_saved", job_id=self.job_id, stage="saving", filename=name,
                  bytes=report_path.stat().st_size)
            self.recorder.end(handle, "done")
        except Cancelled:
            self.recorder.end(handle, "cancelled", {"error_code": "cancelled"})
            raise
        except Exception:
            self.recorder.end(handle, "failed", {"error_code": "report_save_failed"})
            raise

    # Helpers ------------------------------------------------------------------

    def _phase(self, code, label, percent=None, **args):
        """Persist stable phase identifiers alongside the historical English label."""
        self.model.check_cancelled()
        self.phase_args, self.phase_label = args, label
        values = {"stage": label, "phase_code": code, "phase_args": json.dumps(args)}
        if percent is not None and percent != self.last_percent:
            values["progress_percent"] = percent
            self.last_percent = percent
        self.store.update_job(self.job_id, **values)
        # Page extraction can be frequent: phase changes belong in INFO, page detail in DEBUG.
        event("job_phase", logging.DEBUG if code in ("extraction", "ocr") else logging.INFO,
              job_id=self.job_id, stage=code, progress_percent=percent, **args)
        if code != "saving":
            self._write_preview(code, content="", reasoning="", active=False, metrics={})

    def _write_preview(self, phase_code, **fields):
        """Publish the live preview file read by the /live route, when enabled."""
        if self.runtime.live_output:
            atomic_json(self.preview_path, {"phase": self.phase_label, "phase_code": phase_code,
                                            "phase_args": self.phase_args, "updated_at": now(), **fields})

    def _step(self, name, stage, dependencies, compute):
        """Reuse a verified artifact, or run compute() and save its text.

        Every outcome (reused, done, failed or cancelled) is recorded with its
        measurements, so failed attempts remain visible in the metrics.
        """
        model = self.model
        model.check_cancelled()
        generation = stage in GENERATION_STAGES
        if generation:
            model.current_metrics = {}
            # Pre-fix artifacts may contain Latin-1-decoded UTF-8 even when
            # their file hashes are valid. Recompute on an explicit retry;
            # never try to repair their Unicode strings heuristically.
            dependencies = {**dependencies, "text_transport_version": 1,
                            "reliability_version": reliability.VERSION}
        handle = self.recorder.start(stage, name)
        before_calls, before_ms = model.tokenization_calls, model.tokenization_ms
        before_hits = model.tokenization_cache_hits
        try:
            cached = self.artifacts.read(name, dependencies)
            if cached is not None:
                self.recorder.end(handle, "reused", {"artifact_reused": True})
                if generation:
                    self._write_preview(stage, content=cached[-self.runtime.preview_chars:],
                                        content_chars=len(cached), reasoning="", reasoning_chars=0,
                                        active=False, metrics={"artifact_reused": True})
                return cached
            result = compute()
            model.check_cancelled()
            self.artifacts.write(name, result, dependencies)
            measurements = dict(model.current_metrics) if generation else {}
            measurements.update(tokenization_calls=model.tokenization_calls - before_calls,
                                tokenization_ms=model.tokenization_ms - before_ms,
                                tokenization_cache_hits=model.tokenization_cache_hits - before_hits)
            self.recorder.end(handle, "done", measurements)
            return result
        except Exception as exc:
            measurements = dict(model.current_metrics) if generation else {}
            measurements["error_code"] = getattr(exc, "code", "step_failed")
            self.recorder.end(handle, "cancelled" if isinstance(exc, Cancelled) else "failed", measurements)
            raise

    def _generate(self, stage, instruction, text, budget, source_pages):
        """Call the model once within the call budget, publishing bounded previews."""
        if self.calls >= self.config["max_calls"]:
            raise ProcessingError("call_limit", "The job exceeded its generation-call limit.")
        self.calls += 1
        model = self.model
        model.current_stage = stage
        started, started_at = time.monotonic(), now()
        model.current_metrics = {}
        limit = self.runtime.preview_chars

        def live(content, reasoning, completed):
            """Write a cheap public preview, keeping useful text and reasoning separate."""
            if not self.runtime.live_output:
                return
            stats = dict(model.current_metrics)
            self._write_preview(stage, content=content[-limit:], reasoning=reasoning[-limit:],
                                content_chars=stats.get("content_chars", len(content)),
                                reasoning_chars=stats.get("reasoning_chars", len(reasoning)),
                                active=not completed, started_at=started_at,
                                elapsed_seconds=round(time.monotonic() - started, 1), metrics=stats)

        live("", "", False)
        answer = model.complete(instruction, text, budget, on_update=live)
        # Public metrics contain counts only; detailed source values stay local.
        check = reliability.audit(answer, self.evidence, source_pages)
        # Retain historical recall counters for benchmark/API compatibility.
        # These raw-string differences are not numerical or citation validation.
        references = set(reliability.PAGE_REFERENCE.findall(text))
        output_references = set(reliability.PAGE_REFERENCE.findall(answer))
        numbers = set(RAW_NUMBER.findall(text))
        output_numbers = set(RAW_NUMBER.findall(answer))
        model.current_metrics["quality_checks"] = {
            "source": "mechanical_warning_only", "semantic_validation": False,
            "warnings": len(check["issues"]),
            "numeric_checks": check["numeric"]["checked"],
            "missing_page_references": len(references - output_references),
            "unseen_page_references": len(output_references - references),
            "missing_numeric_items": len(numbers - output_numbers)}
        return answer

    def _check(self, name, text, source_pages, sections, evidence_limit=None):
        """Run the reliability checks on a generated text, new or reused, and save the results.

        The results, with the pages and sections the step covered, go to
        quality_checks.json rather than into the Markdown text.
        """
        check = reliability.audit(text, self.evidence, source_pages)
        check.update(input_pages=sorted(set(source_pages)), source_sections=sorted(set(sections)))
        if evidence_limit is not None:
            check["evidence_prompt_limit"] = evidence_limit
        self.quality["steps"][name] = check
        atomic_json(self.artifacts.root / "quality_checks.json", self.quality)
        return check
