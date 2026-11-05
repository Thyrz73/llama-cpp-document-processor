// The card of one analysis in the jobs list.
import { el } from "./dom.js";
import { state, ACTIVE_STATUSES, TERMINAL_STATUSES } from "./state.js";
import { t, errorText, phaseLabel, statusLabel } from "./i18n.js";
import { confirmDeletion, updateSelection } from "./history.js";
import { createLive } from "./live.js";
import { showMetrics } from "./metrics.js";

const SVG = "http://www.w3.org/2000/svg";

// Build the card of job.
// handlers.retry, handlers.cancel and handlers.toggleLive come from jobs.js:
// they update the list, and jobs.js imports this module. live is the open
// live panel, reused so that its text and scroll position survive a refresh.
export function jobCard(job, handlers, live) {
  const item = el("article", undefined, "item");
  item.dataset.jobId = job.id;
  item.append(cardHeader(job));
  item.append(el("div", phaseLabel(job), "meta"));
  if (job.configuration)
    item.append(
      el(
        "div",
        t("Profile: {profile}", {
          profile: t("profile_name." + job.configuration.profile),
        }) + (job.configuration.customized ? " · " + t("Customized") : ""),
        "meta",
      ),
    );
  item.append(
    el(
      "div",
      t("{pages} pages · {done}/{total} sections · created {date}", {
        pages: job.pages || "?",
        done: job.done_chunks || 0,
        total: job.total_chunks || "?",
        date: new Date(job.created_at).toLocaleString(state.language),
      }),
      "meta",
    ),
  );
  appendProgress(item, job);
  if (job.error) item.append(el("div", errorText(job, job.error), "error"));
  const reportActions = el("div", undefined, "actions report-actions");
  if (job.status === "done" && job.report_available !== false) {
    item.append(
      el("div", job.report_name || t("Markdown report"), "meta report-name"),
    );
    reportActions.append(...reportLinks(job));
  } else if (job.status === "done") {
    item.append(el("div", t("Report unavailable"), "meta"));
  }
  item.append(actionButtons(job, handlers, item));
  // The saved request is the only prompt a user can write; server prompts stay
  // private. The server sets question_custom to 0 for default instructions,
  // older jobs included.
  if (job.question && job.question_custom !== 0) item.append(savedRequest(job));
  if (state.expandedId === job.id) item.append(live || createLive(job.id));
  if (reportActions.children.length) item.append(reportActions);
  return item;
}

// File name and status, with selection and deletion for finished analyses.
function cardHeader(job) {
  const top = el("div", undefined, "top");
  const finished = TERMINAL_STATUSES.has(job.status);
  const identity = el(
    finished && state.selectionMode ? "label" : "div",
    undefined,
    "job-identity",
  );
  if (finished) {
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.dataset.selectJob = job.id;
    checkbox.setAttribute(
      "aria-label",
      t("Select analysis: {name}", { name: job.filename }),
    );
    checkbox.addEventListener("change", () => {
      if (state.selectionMode && !state.deleting) {
        if (checkbox.checked) state.selectedJobs.add(job.id);
        else state.selectedJobs.delete(job.id);
      }
      updateSelection();
    });
    identity.append(checkbox);
  }
  identity.append(el("span", job.filename, "name"));
  top.append(identity);
  const status = el("div", undefined, "job-status");
  status.append(el("span", statusLabel(job.status), `pill ${job.status}`));
  if (finished) status.append(deleteButton(job));
  top.append(status);
  return top;
}

// Trash button that asks to confirm the deletion of one analysis.
function deleteButton(job) {
  const remove = el("button", undefined, "button-danger button-icon");
  remove.type = "button";
  remove.title = t("Delete analysis");
  remove.setAttribute(
    "aria-label",
    t("Delete analysis: {name}", { name: job.filename }),
  );
  remove.dataset.deleteJob = job.id;
  remove.disabled = state.deleting;
  remove.hidden = state.selectionMode;
  const icon = document.createElementNS(SVG, "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("class", "trash-icon");
  icon.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7");
  icon.append(path);
  remove.append(icon);
  remove.addEventListener("click", () =>
    confirmDeletion([job.id], false, remove),
  );
  return remove;
}

// Progress bar and percentage. A completed job shows 100 %; any other job
// stays between 0 and 99.
function appendProgress(item, job) {
  const pct =
    job.status === "done"
      ? 100
      : Math.max(0, Math.min(99, Number(job.progress_percent || 0)));
  const track = el("div", undefined, "track"),
    fill = el("div", undefined, "fill");
  fill.style.width = pct + "%";
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", t("Workflow progress"));
  track.setAttribute("aria-valuenow", pct);
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.append(fill);
  item.append(track);
  item.append(el("div", pct + t("% of workflow completed"), "micro muted"));
}

// "View" opens the rendered report in a new tab; "Download" saves the Markdown.
function reportLinks(job) {
  const view = el("a", t("View"), "button-secondary report-button");
  view.href = `/api/jobs/${job.id}/report/view?lang=${state.language}`;
  view.target = "_blank";
  view.rel = "noopener noreferrer";
  const link = el("a", t("Download"), "button-secondary report-button");
  link.href = `/api/jobs/${job.id}/report`;
  link.download = job.report_name || "";
  return [view, link];
}

// Retry, cancel, metrics and live output buttons.
function actionButtons(job, handlers, item) {
  const actions = el("div", undefined, "actions");
  if (["failed", "cancelled"].includes(job.status)) {
    const retry = el("button", t("Retry"), "button-secondary");
    retry.type = "button";
    retry.disabled = state.deleting;
    retry.addEventListener("click", () => handlers.retry(job.id));
    actions.append(retry);
  }
  if (ACTIVE_STATUSES.has(job.status)) {
    const pending = state.cancellingJobs.has(job.id);
    const cancel = el(
      "button",
      t(
        job.cancel_requested
          ? "Cancellation requested"
          : pending
            ? "Requesting cancellation…"
            : "Cancel",
      ),
      "button-danger",
    );
    cancel.disabled = !!job.cancel_requested || pending;
    cancel.setAttribute("aria-busy", String(pending));
    cancel.type = "button";
    cancel.addEventListener("click", () => handlers.cancel(job.id));
    actions.append(cancel);
  }
  const stats = el("button", t("Metrics"), "button-secondary");
  stats.type = "button";
  stats.addEventListener("click", () => showMetrics(job.id, item));
  actions.append(stats);
  const open = state.expandedId === job.id;
  const liveButton = el(
    "button",
    t(open ? "Hide live output" : "Show live output"),
    "button-secondary",
  );
  liveButton.type = "button";
  liveButton.setAttribute("aria-expanded", String(open));
  liveButton.addEventListener("click", () => handlers.toggleLive(job.id));
  actions.append(liveButton);
  return actions;
}

// Collapsible block with the request saved for the job, shown as plain text.
function savedRequest(job) {
  const details = el("details", undefined, "custom-prompt");
  details.open = state.promptExpanded.has(job.id);
  details.append(
    el(
      "summary",
      t(
        job.question_custom === 1
          ? "Custom analysis instructions"
          : "Saved analysis instructions",
      ),
    ),
  );
  details.append(el("pre", job.question, "prompt-text"));
  details.addEventListener("toggle", () => {
    if (details.open) state.promptExpanded.add(job.id);
    else state.promptExpanded.delete(job.id);
  });
  return details;
}
