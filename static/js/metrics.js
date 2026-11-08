// Metrics panel: the attempts and step measurements of one job, loaded only
// when the user asks for them, outside the frequent list polling.
import { $, el } from "./dom.js";
import { t, errorText, statusLabel } from "./i18n.js";
import { jsonFetch } from "./api.js";

// Format a duration in milliseconds as seconds, or return fallback.
const seconds = (ms, fallback) =>
  ms == null ? fallback : (ms / 1000).toFixed(1);

// Show the metrics panel of job id inside its card (item), or toggle it
// when it is already loaded.
export async function showMetrics(id, item) {
  const previous = $("metrics-" + id);
  if (previous) {
    previous.hidden = !previous.hidden;
    return;
  }
  const output = el("div", t("Loading…"), "metrics");
  output.id = "metrics-" + id;
  item.insertBefore(output, item.querySelector(".report-actions"));
  try {
    const response = await jsonFetch(`/api/jobs/${id}/metrics`);
    const data = await response.json();
    if (!response.ok) throw Error(errorText(data, "Metrics unavailable"));
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
  } catch (error) {
    output.textContent = error.message;
  }
}
