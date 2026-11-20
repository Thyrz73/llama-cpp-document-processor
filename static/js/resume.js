// Resume only edits saved output budgets. The dialog lives outside the job
// cards so polling and language changes never replace its inputs or focus.
import { $, el } from "./dom.js";
import { state } from "./state.js";
import { t, errorText, phaseLabel } from "./i18n.js";
import { focusHistoryControl } from "./history.js";

const BUDGETS = {
  analysis_tokens: "Analysis output tokens",
  merge_tokens: "Merge output tokens",
  final_tokens: "Final output tokens",
};
const PHASES = new Set([
  "preparing",
  "extraction",
  "ocr",
  "chunking",
  "section",
  "merge",
  "final",
  "saving",
]);
const PHASE_BUDGET = {
  section: "analysis_tokens",
  merge: "merge_tokens",
  final: "final_tokens",
};
const SAVED_SETTINGS = {
  profile: "Performance profile",
  report_language: "Report language",
  detail_level: "Report detail",
  thinking_section: "Section reasoning",
  thinking_merge: "Merge reasoning",
  thinking_final: "Final reasoning",
  temperature: "Temperature",
  top_p: "top_p",
  top_k: "top_k",
  min_p: "min_p",
  concurrency: "Concurrency",
  step_timeout_seconds: "Generation time limit (seconds)",
  max_calls: "Maximum model calls",
};
const SETTING_VALUES = {
  auto: "Follow the request",
  en: "English",
  fr: "French",
  concise: "Concise",
  standard: "Standard",
  detailed: "Detailed",
};

const inputFor = (name) => $("resume_" + name);
const pending = () => state.restartingJobs.has(state.resumeIntent?.job.id);
const canAdjust = (config) =>
  Object.keys(BUDGETS).every(
    (name) => Number.isSafeInteger(config?.[name]) && config[name] > 0,
  );
const version = (job) =>
  JSON.stringify([job.status, job.queued_at, job.configuration]);

// These listeners are installed once at startup, never on dialog opening.
// Native showModal supplies the focus trap and keyboard traversal.
export function initResumeDialog(submit) {
  if (!$("resumeDialog")) return;
  $("resumeCancel").addEventListener("click", closeResumeDialog);
  $("resumeDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeResumeDialog();
  });
  $("resumeRetry").addEventListener("click", () => submit(false));
  $("resumeForm").addEventListener("submit", (event) => {
    event.preventDefault();
    submit(true);
  });
}

export function openResumeDialog(id, trigger) {
  if (state.deleting || state.deleteIntent || state.resumeIntent) return;
  const job = state.jobs.find((job) => job.id === id);
  if (!job || !["failed", "cancelled"].includes(job.status)) return;
  state.resumeIntent = {
    job: { ...job, configuration: { ...job.configuration } },
    version: version(job),
    stale: false,
    error: null,
  };
  state.resumeTrigger = trigger;
  for (const name of Object.keys(BUDGETS)) {
    const input = inputFor(name);
    input.value = String(job.configuration?.[name] ?? "");
    input.min = String(job.configuration?.[name] ?? 1);
    input.setAttribute("aria-invalid", "false");
  }
  renderResumeDialog();
  $("resumeDialog").showModal();
  $("resumeCancel").focus();
}

export function closeResumeDialog() {
  if (!state.resumeIntent || pending()) return;
  const id = state.resumeIntent.job.id;
  const trigger = state.resumeTrigger;
  state.resumeIntent = null;
  state.resumeTrigger = null;
  $("resumeDialog").close();
  const replacement = [...$("jobs").querySelectorAll("[data-resume-job]")].find(
    (node) => node.dataset.resumeJob === id,
  );
  focusHistoryControl(trigger?.isConnected ? trigger : replacement);
}

// Do not silently rebase edits onto a changed configuration or another attempt.
// The backend remains authoritative for races after the last list response.
export function syncResumeDialog() {
  const intent = state.resumeIntent;
  if (!intent) return;
  if (!pending()) {
    const current = state.jobs.find((job) => job.id === intent.job.id);
    if (!current || version(current) !== intent.version) intent.stale = true;
  }
  renderResumeDialog();
}

export function renderResumeDialog() {
  const intent = state.resumeIntent;
  if (!intent) return;
  const { job } = intent;
  const config = job.configuration;
  const knownPhase = PHASES.has(job.phase_code);
  $("resumeDocument").textContent = job.filename;
  $("resumeFailure").textContent =
    job.status === "cancelled"
      ? t("This analysis was cancelled.")
      : errorText(
          job,
          "Analysis failed. Check the PDF, configuration and local diagnostics.",
        );
  $("resumePhase").textContent = knownPhase
    ? t("Last processing phase: {phase}", { phase: phaseLabel(job) })
    : t("The failed or interrupted processing phase is unavailable.");
  const budget = knownPhase && PHASE_BUDGET[job.phase_code];
  $("resumeGuidance").textContent =
    job.error_code === "output_limit"
      ? t(
          budget
            ? "If the output budget was exhausted, increasing {budget} may help. Increasing output budgets does not enlarge the context window or guarantee success."
            : "If the output budget was exhausted, increasing the budget for the affected phase may help. Increasing output budgets does not enlarge the context window or guarantee success.",
          { budget },
        )
      : ["context_insufficient", "merge_impossible"].includes(job.error_code)
        ? t(
            "This is a context-window limitation. Increasing output budgets may leave less room for input. Create a new analysis with smaller inputs or suitable context settings.",
          )
        : "";
  $("resumeConfig").textContent = canAdjust(config)
    ? t(
        "Section {chunk} · analysis {analysis} · merge {merge} · final {final} · context limit {context}",
        {
          chunk: config.chunk_tokens ?? t("Unknown"),
          analysis: config.analysis_tokens,
          merge: config.merge_tokens,
          final: config.final_tokens,
          context: config.context_tokens ?? t("Unknown"),
        },
      )
    : t(
        "Saved budgets are unavailable. Retry with current settings is still available.",
      );
  const settings = $("resumeSavedSettings");
  settings.replaceChildren();
  for (const [name, label] of Object.entries(SAVED_SETTINGS)) {
    if (config[name] === undefined) continue;
    const value = config[name];
    settings.append(
      el("dt", t(label)),
      el(
        "dd",
        value === null
          ? t("Engine default")
          : name === "profile"
            ? t("profile_name." + value)
            : t(SETTING_VALUES[value] || String(value)),
      ),
    );
  }
  $("resumeStale").hidden = !intent.stale;
  const busy = pending();
  $("resumeDialog").setAttribute("aria-busy", String(busy));
  $("resumeRetry").disabled = busy || intent.stale;
  $("resumeAdjusted").disabled = busy || intent.stale || !canAdjust(config);
  $("resumeCancel").disabled = busy;
  for (const name of Object.keys(BUDGETS))
    inputFor(name).disabled = busy || intent.stale || !canAdjust(config);
  $("resumeStatus").textContent = busy ? t("Submitting…") : "";
  $("resumeError").hidden = !intent.error;
  if (intent.error) {
    const args = { ...intent.error.args };
    if (args.field) args.field = t(args.field);
    $("resumeError").textContent = intent.error.key
      ? t(intent.error.key, args)
      : errorText(intent.error.data, intent.error.fallback || "Resume failed");
  }
}

// Strict positive decimal integers, at least the saved value. The server owns
// range/context validation. No settings or job snapshots are mutated here.
export function modifiedBudgets() {
  const intent = state.resumeIntent;
  if (!intent || !canAdjust(intent.job.configuration)) return null;
  const changes = {};
  intent.error = null;
  for (const [name, label] of Object.entries(BUDGETS)) {
    const input = inputFor(name);
    const raw = input.value.trim();
    const value = Number(raw);
    const minimum = intent.job.configuration[name];
    const valid =
      /^\d+$/.test(raw) && Number.isSafeInteger(value) && value >= minimum;
    input.setAttribute("aria-invalid", String(!valid));
    if (!valid && !intent.error) {
      intent.error = {
        key: "{field} must be a positive whole number of at least {minimum}.",
        args: { field: label, minimum },
      };
      input.focus();
    }
    if (valid && value > minimum) changes[name] = value;
  }
  if (!intent.error && !Object.keys(changes).length)
    intent.error = {
      key: "Increase at least one output budget, or retry with current settings.",
    };
  renderResumeDialog();
  return intent.error ? null : changes;
}
