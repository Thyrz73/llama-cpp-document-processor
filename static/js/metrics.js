// Metrics belong to their panel node, never to a global response cache.
import { $, el } from "./dom.js";
import { state } from "./state.js";
import { t, errorText, statusLabel } from "./i18n.js";
import { jsonFetch } from "./api.js";

const CONTEXT = Symbol("metrics context");
const seconds = (ms, fallback) =>
  ms == null ? fallback : (ms / 1000).toFixed(1);

// Queue/configuration changes identify retries/resumes; recorded job updates
// invalidate observations that may predate newer step or attempt measurements.
function version(job) {
  return JSON.stringify([
    job.id,
    job.created_at,
    job.queued_at,
    job.updated_at,
    job.status,
    job.done_chunks,
    job.total_chunks,
    job.phase_code,
    job.phase_args,
    job.progress_percent,
    job.latest_attempt_id,
    job.configuration,
  ]);
}

function current(output, observedJob) {
  const context = output[CONTEXT];
  if (!context) return false;
  const job = observedJob || state.jobs.find((job) => job.id === context.id);
  return (
    context.valid &&
    state.uiActive &&
    context.generation === state.uiGeneration &&
    output.isConnected &&
    $(output.id) === output &&
    job &&
    context.version === version(job)
  );
}

export function invalidateMetrics(id) {
  const output = $("metrics-" + id);
  if (!output) return;
  const context = output[CONTEXT];
  if (context) {
    context.valid = false;
    context.data = null;
    context.error = null;
  }
  output.parentElement
    ?.querySelector("[data-metrics-job]")
    ?.setAttribute("aria-expanded", "false");
  output.remove();
}

// Called before the list is detached. Only the same job observation may retain
// the node, including its hidden state and an in-flight request's identity.
export function retainMetrics(job) {
  const output = $("metrics-" + job.id);
  if (!output) return null;
  if (!current(output, job)) {
    invalidateMetrics(job.id);
    return null;
  }
  if (output[CONTEXT].language !== state.language) renderMetrics(output);
  return output;
}

function renderMetrics(output) {
  const context = output[CONTEXT];
  context.language = state.language;
  if (context.phase === "loading") {
    output.textContent = t("Loading…");
    return;
  }
  if (context.phase === "error") {
    output.textContent = context.error.data
      ? errorText(context.error.data, "Metrics unavailable")
      : t(context.error.key || context.error.message);
    return;
  }
  const data = context.data;
  output.replaceChildren();
  for (const run of data.runs)
    output.append(
      el(
        "p",
        t("Attempt {id}: {status} · processing {seconds}s · queue {queue}s", {
          id: run.id,
          status: statusLabel(run.status),
          seconds: seconds(run.duration_ms, t("In progress")),
          queue: seconds(run.queue_ms, t("Unknown")),
        }),
      ),
    );
  for (const step of [...data.steps].reverse()) {
    const m = step.metrics;
    output.append(
      el(
        "div",
        [
          step.step_key,
          t(step.status),
          m.duration_ms == null ? "—" : seconds(m.duration_ms) + "s",
          t("Generated: {tokens} tokens", { tokens: m.output_tokens ?? "—" }),
          m.finish_reason || m.error_code || "—",
        ].join(" · "),
      ),
    );
  }
  if (!data.steps.length)
    output.textContent = t("No measurements for this historical job.");
  if (data.next_before)
    output.append(
      el(
        "p",
        t(
          "Showing the latest 200 steps. Older measurements remain available through the API.",
        ),
      ),
    );
}

// Toggle an owned panel, or issue its single initial request. Moving its card
// during that request is safe; removal, invalidation and page changes are not.
export async function showMetrics(id, item) {
  const job = state.jobs.find((job) => job.id === id);
  if (
    !state.uiActive ||
    !job ||
    !item.isConnected ||
    item.dataset.jobId !== id ||
    !$("jobs")?.contains(item)
  )
    return;
  const previous = retainMetrics(job);
  const button = item.querySelector("[data-metrics-job]");
  if (previous) {
    previous.hidden = !previous.hidden;
    button?.setAttribute("aria-expanded", String(!previous.hidden));
    return;
  }
  const output = el("div", undefined, "metrics");
  output.id = "metrics-" + id;
  const context = (output[CONTEXT] = {
    id,
    version: version(job),
    generation: state.uiGeneration,
    valid: true,
    phase: "loading",
    data: null,
    error: null,
    language: state.language,
  });
  renderMetrics(output);
  item.insertBefore(output, item.querySelector(".report-actions"));
  button?.setAttribute("aria-expanded", "true");
  try {
    const response = await jsonFetch(`/api/jobs/${id}/metrics`);
    const data = await response.json();
    if (!current(output)) return;
    if (!response.ok) {
      context.phase = "error";
      context.error = { data: data && typeof data === "object" ? data : {} };
    } else {
      if (!data || !Array.isArray(data.runs) || !Array.isArray(data.steps))
        throw Error(t("Metrics unavailable"));
      context.phase = "ready";
      context.data = data;
    }
    renderMetrics(output);
  } catch (error) {
    if (!current(output)) return;
    context.phase = "error";
    context.data = null;
    context.error = {
      message: error.message,
      key: ["Request timed out.", "Metrics unavailable"].find(
        (key) => t(key) === error.message,
      ),
    };
    renderMetrics(output);
  }
}
