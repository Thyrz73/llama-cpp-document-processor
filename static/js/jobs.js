// The analyses list: loading and polling it, rendering it, and the actions
// that change a job (retry, cancel, delete) before reloading the list.
import { $, el } from "./dom.js";
import { state, ACTIVE_STATUSES, TERMINAL_STATUSES } from "./state.js";
import { t, errorText } from "./i18n.js";
import { ApiError, jsonFetch } from "./api.js";
import { jobCard } from "./card.js";
import { invalidateLivePreview, syncLivePolling } from "./live.js";
import {
  openResumeDialog,
  closeResumeDialog,
  renderResumeDialog,
  syncResumeDialog,
  modifiedBudgets,
} from "./resume.js";
import {
  closeDeleteDialog,
  historyControls,
  historyHeading,
  focusHistoryControl,
  updateSelection,
} from "./history.js";

// While a job is active the list is reloaded every 6 seconds. After failed
// reloads the wait doubles, up to 30 seconds.
const POLL_MS = 6000;
const MAX_POLL_MS = 30000;
const GROUPS = [
  ["active", "Active analyses", "No analyses in progress."],
  ["history", "Analysis history", "No analysis history yet."],
];
const CARD_HANDLERS = {
  resume: openResumeDialog,
  cancel: cancelJob,
  toggleLive,
};

// Cancel the planned automatic reload, if any.
export function stopJobsPolling() {
  if (state.jobsTimer !== null) clearTimeout(state.jobsTimer);
  state.jobsTimer = null;
}

// Plan the next automatic reload while a job is active and the tab is visible.
function scheduleJobsPolling() {
  stopJobsPolling();
  if (state.uiActive && state.hasActiveJobs && !document.hidden)
    state.jobsTimer = setTimeout(
      refresh,
      Math.min(MAX_POLL_MS, POLL_MS * 2 ** Math.max(0, state.jobsFailures - 1)),
    );
}

// Reload the list; concurrent callers share one request and its promise.
// afterChange === true means a job was just created or changed: a response
// to an older request cannot show that change, so the list is read again.
// (The Refresh button passes its click event instead.) On errors the cards
// stay visible; polling adapts once the request settles.
export function refresh(afterChange = false) {
  if (!state.uiActive) return Promise.resolve();
  const generation = state.uiGeneration;
  if (afterChange === true) {
    state.hasActiveJobs = true;
    state.refreshPending = true;
  }
  if (state.jobsRequest) return state.jobsRequest;
  stopJobsPolling();
  state.jobsRequest = (async () => {
    do {
      state.refreshPending = false;
      try {
        const res = await jsonFetch("/api/jobs", { cache: "no-store" });
        if (!res.ok) throw Error(t("Unable to load jobs"));
        const data = await res.json();
        if (generation !== state.uiGeneration) return;
        if (state.refreshPending) continue;
        if (!validHistoryResponse(data))
          throw new ApiError("error.invalid_response", data);
        const hasActiveJobs =
          data.has_active_jobs ??
          data.jobs.some((job) => ACTIVE_STATUSES.has(job.status));
        // Commit the validated snapshot together; failures retain the last list.
        state.jobs = data.jobs;
        state.hasActiveJobs = hasActiveJobs;
        state.jobsFailures = 0;
        $("jobsError").hidden = true;
        renderJobs();
      } catch (err) {
        if (generation !== state.uiGeneration) return;
        state.jobsFailures++;
        const error = $("jobsError"),
          message = t("Cannot refresh analyses: ") + err.message;
        if (error.textContent !== message) error.textContent = message;
        error.hidden = false;
      }
    } while (state.refreshPending);
  })().finally(() => {
    if (generation !== state.uiGeneration) return;
    state.jobsRequest = null;
    // A change can be reported between the end of the loop and this cleanup.
    if (state.refreshPending) return refresh(true);
    scheduleJobsPolling();
  });
  return state.jobsRequest;
}

// Rebuild the cards when the jobs, the open live panel or the deletion state
// changed. The open live panel is moved into the new card instead of being
// rebuilt, so its text, tab and scroll position are kept.
export function renderJobs() {
  syncResumeDialog();
  const eligible = new Set(
    state.jobs
      .filter((job) => TERMINAL_STATUSES.has(job.status))
      .map((job) => job.id),
  );
  for (const id of state.selectedJobs)
    if (!eligible.has(id)) state.selectedJobs.delete(id);
  const signature = JSON.stringify([
    state.jobs,
    state.expandedId,
    state.deleting,
    state.selectionMode,
    [...state.restartingJobs],
  ]);
  if (signature === state.renderedSignature) {
    syncLivePolling();
    return;
  }
  const focused = document.activeElement;
  const live = state.expandedId ? $("live-" + state.expandedId) : null,
    host = $("jobs");
  const priorScroll = live ? $("live-content")?.scrollTop : null;
  host.replaceChildren();
  state.renderedSignature = signature;
  const groups = {};
  for (const [key, heading, empty] of GROUPS) {
    const section = el("section", undefined, "job-group job-group-" + key);
    const title = el("h3", t(heading));
    title.id = "jobs-" + key + "-heading";
    section.setAttribute("aria-labelledby", title.id);
    section.append(
      key === "history" ? historyHeading(title, renderJobs) : title,
    );
    const list = el("div", undefined, "job-list");
    section.append(list);
    // The toolbar sits above the history cards, after the heading.
    if (key === "history") {
      const { controls, actions } = historyControls(eligible);
      list.append(controls);
      section.append(actions);
    }
    host.append(section);
    groups[key] = list;
    if (
      !state.jobs.some(
        (job) => ACTIVE_STATUSES.has(job.status) === (key === "active"),
      )
    )
      list.append(el("p", t(empty), "muted"));
  }
  for (const job of state.jobs)
    groups[ACTIVE_STATUSES.has(job.status) ? "active" : "history"].append(
      jobCard(job, CARD_HANDLERS, live),
    );
  // Browsers may reset scrollTop when a kept node is detached and reattached.
  if (priorScroll != null && $("live-content"))
    $("live-content").scrollTop = priorScroll;
  updateSelection();
  restoreFocus(focused, host);
  syncLivePolling();
}

// Give the keyboard focus back to the control that had it before a rebuild,
// so progress updates do not interrupt a selection in the history.
function restoreFocus(focused, host) {
  const selector = focused?.dataset?.selectJob
    ? "[data-select-job]"
    : focused?.dataset?.deleteJob
      ? "[data-delete-job]"
      : focused?.dataset?.resumeJob
        ? "[data-resume-job]"
        : null;
  if (selector) {
    const replacement = [...host.querySelectorAll(selector)].find(
      (node) =>
        node.dataset.selectJob === focused.dataset.selectJob &&
        node.dataset.deleteJob === focused.dataset.deleteJob &&
        node.dataset.resumeJob === focused.dataset.resumeJob,
    );
    focusHistoryControl(replacement);
  } else if (
    [
      "toggleSelection",
      "selectAllJobs",
      "clearSelection",
      "deleteSelection",
    ].includes(focused?.id)
  ) {
    const replacement = $(focused.id);
    focusHistoryControl(replacement);
  }
}

// Rebuild the cards even when the jobs did not change, for example after a
// language change or while a cancellation request is pending.
export function rerenderJobs() {
  state.renderedSignature = null;
  renderJobs();
}

// Open or close the live panel of job id.
export function toggleLive(id) {
  state.expandedId = state.expandedId === id ? null : id;
  state.liveTab = "content";
  renderJobs();
}

// Show the outcome of a job action above the list, or hide it when notice
// is null. notice is { error } or the counts of a deletion.
function showJobsNotice(notice) {
  state.jobsNotice = notice;
  renderJobsNotice();
}

// Render the current notice in the interface language.
export function renderJobsNotice() {
  const node = $("jobsNotice"),
    notice = state.jobsNotice;
  if (!node) return;
  node.hidden = !notice;
  if (!notice) return;
  node.className =
    "jobs-notice" +
    (notice.error || notice.failed || notice.warnings ? " warning" : "");
  node.textContent =
    notice.error || t("Deleted: {count}.", { count: notice.deleted });
  if (notice.failed)
    node.textContent +=
      " " +
      t("Could not delete: {count}.", { count: notice.failed }) +
      " " +
      notice.reasons.map((code) => t("error." + code)).join(" ");
  if (notice.warnings)
    node.textContent +=
      " " + t("Some associated files were retained. Check the server storage.");
}

// Queue a failed or cancelled job again, then reload the list. A refusal,
// such as an exhausted retry limit, is shown above the list.
export async function retry(id) {
  return restartJob(id);
}

// Called by the dialog's two actions. Retry sends no body; resume sends only
// explicitly increased output budgets, never the new-analysis form settings.
export async function submitResume(adjusted) {
  const intent = state.resumeIntent;
  if (!intent || intent.stale || state.restartingJobs.has(intent.job.id))
    return;
  const budgets = adjusted ? modifiedBudgets() : undefined;
  if (adjusted && !budgets) return;
  return restartJob(intent.job.id, budgets);
}

const objectPayload = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const stringArray = (value) =>
  Array.isArray(value) && value.every((item) => typeof item === "string");

function validHistoryJob(job) {
  if (
    !objectPayload(job) ||
    typeof job.id !== "string" ||
    !/^[a-f0-9]{32}$/.test(job.id) ||
    typeof job.filename !== "string" ||
    !job.filename ||
    (!ACTIVE_STATUSES.has(job.status) && !TERMINAL_STATUSES.has(job.status)) ||
    typeof job.created_at !== "string" ||
    !Number.isFinite(Date.parse(job.created_at))
  )
    return false;
  // Older jobs may omit these fields or retain nullable database columns.
  if (
    [
      "question",
      "error",
      "error_code",
      "phase_code",
      "stage",
      "phase",
      "queued_at",
      "updated_at",
      "report_name",
    ].some((name) => job[name] != null && typeof job[name] !== "string") ||
    ["pages", "done_chunks", "total_chunks", "progress_percent"].some(
      (name) => job[name] != null && !Number.isFinite(job[name]),
    ) ||
    (job.report_available != null &&
      typeof job.report_available !== "boolean") ||
    (job.cancel_requested != null &&
      ![true, false, 0, 1].includes(job.cancel_requested))
  )
    return false;
  if (job.configuration != null) {
    if (!objectPayload(job.configuration)) return false;
    // Public controls are scalars plus warning codes, never private snapshots.
    if (
      !Object.entries(job.configuration).every(([name, value]) =>
        name === "warnings"
          ? stringArray(value)
          : value === null ||
            ["string", "boolean"].includes(typeof value) ||
            Number.isFinite(value),
      )
    )
      return false;
  }
  if (job.phase_args != null) {
    if (!objectPayload(job.phase_args)) return false;
    const failureCode = job.phase_args.failed_phase?.code;
    if (failureCode != null && typeof failureCode !== "string") return false;
    // Phase arguments include page arrays and failure metadata. Preserve their
    // fallback semantics, but reject values that cannot be rendered as text.
    try {
      for (const value of Object.values(job.phase_args)) String(value);
    } catch {
      return false;
    }
  }
  return true;
}

function validHistoryResponse(data) {
  if (
    !objectPayload(data) ||
    !Array.isArray(data.jobs) ||
    (data.has_active_jobs !== undefined &&
      typeof data.has_active_jobs !== "boolean")
  )
    return false;
  const ids = new Set();
  return data.jobs.every((job) => {
    if (!validHistoryJob(job) || ids.has(job.id)) return false;
    ids.add(job.id);
    return true;
  });
}

// Match the existing retry/resume contracts; neither endpoint returns a job ID.
function validRestartResponse(data, budgets) {
  if (
    !objectPayload(data) ||
    data.status !== "queued" ||
    Object.hasOwn(data, "error") ||
    Object.hasOwn(data, "error_code")
  )
    return false;
  if (!budgets) return stringArray(data.warnings);
  const config = data.configuration;
  return (
    objectPayload(config) &&
    typeof config.fingerprint === "string" &&
    config.fingerprint.trim().length > 0 &&
    ["analysis_tokens", "merge_tokens", "final_tokens"].every(
      (name) => Number.isSafeInteger(config[name]) && config[name] > 0,
    ) &&
    Object.entries(budgets).every(([name, value]) => config[name] === value) &&
    stringArray(data.validation_warnings)
  );
}

async function restartJob(id, budgets) {
  if (state.deleting || state.restartingJobs.has(id)) return;
  const generation = state.uiGeneration;
  const intent = state.resumeIntent?.job.id === id ? state.resumeIntent : null;
  state.restartingJobs.add(id);
  if (intent) intent.error = null;
  showJobsNotice(null);
  renderJobs();
  let success = false;
  try {
    const r = await jsonFetch(
      `/api/jobs/${id}/${budgets ? "resume" : "retry"}`,
      {
        method: "POST",
        ...(budgets
          ? {
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(budgets),
            }
          : {}),
      },
    );
    const d = await r.json();
    if (generation !== state.uiGeneration) return;
    if (!r.ok) {
      if (intent) {
        intent.error = { data: d, fallback: "Resume failed" };
        // Refresh only after the server reports missing/stale state.
        if ([404, 409].includes(r.status)) {
          intent.stale = true;
          await refresh();
        }
      } else showJobsNotice({ error: errorText(d, "Retry failed") });
      return;
    }
    if (r.status !== 200 || !validRestartResponse(d, budgets))
      throw new ApiError("error.invalid_response", {
        status: r.status,
        data: d,
      });
    invalidateLivePreview(id);
    await refresh(true);
    success = true;
  } catch (err) {
    if (generation !== state.uiGeneration) return;
    const key = err instanceof ApiError ? err.key : "Resume failed";
    if (intent) intent.error = { key, cause: err };
    else showJobsNotice({ error: t(key), cause: err });
  } finally {
    if (generation === state.uiGeneration) {
      state.restartingJobs.delete(id);
      if (success && intent === state.resumeIntent) closeResumeDialog();
      renderJobs();
      renderResumeDialog();
    }
  }
}

// Ask the server to cancel job id, then reload the list. Cancellation is
// cooperative: a running job stops at its next check. Closing the browser
// never cancels a job. A refusal is shown above the list.
export async function cancelJob(id) {
  const current = state.jobs.find((job) => job.id === id);
  if (
    !current ||
    !ACTIVE_STATUSES.has(current.status) ||
    current.cancel_requested ||
    state.cancellingJobs.has(id)
  )
    return;
  state.cancellingJobs.add(id);
  showJobsNotice(null);
  rerenderJobs();
  try {
    const response = await jsonFetch(`/api/jobs/${id}/cancel`, {
      method: "POST",
    });
    const data = await response.json();
    if (!response.ok) throw Error(errorText(data, "Cancellation failed"));
    const job = state.jobs.find((job) => job.id === id);
    if (job) {
      job.cancel_requested = !!data.cancel_requested;
      job.status = data.status;
    }
    await refresh(true);
  } catch (error) {
    showJobsNotice({ error: error.message });
  } finally {
    state.cancellingJobs.delete(id);
    rerenderJobs();
  }
}

// Delete the analyses confirmed in the dialog, then reload the list.
// Deleted cards disappear at once; failures keep their card and selection.
export async function submitDeletion() {
  if (!state.deleteIntent || state.deleting) return;
  const { ids, bulk } = state.deleteIntent;
  closeDeleteDialog();
  showJobsNotice(null);
  state.deleting = true;
  renderJobs();
  try {
    const response = await jsonFetch(
      bulk ? "/api/jobs/delete" : `/api/jobs/${ids[0]}/delete`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: bulk ? JSON.stringify({ ids }) : undefined,
      },
      120000,
    );
    const data = await response.json();
    const failed = "Deletion failed. Refresh history before trying again.";
    if (!response.ok && !data.error_code) throw Error(t(failed));
    const results = bulk ? data.results : [data];
    if (!Array.isArray(results)) throw Error(errorText(data, failed));
    const removed = new Set(
      results
        .filter((result) => result.deleted === true && ids.includes(result.id))
        .map((result) => result.id),
    );
    const failures = results.filter((result) => !result.deleted);
    state.jobsNotice = {
      deleted: removed.size,
      failed: ids.length - removed.size,
      warnings: results.some((result) => result.deleted && result.warning_code),
      reasons: [
        ...new Set(failures.map((result) => result.error_code || "generic")),
      ],
    };
    state.jobs = state.jobs.filter((job) => !removed.has(job.id));
    for (const id of removed) {
      state.selectedJobs.delete(id);
      state.promptExpanded.delete(id);
    }
    if (removed.has(state.expandedId)) state.expandedId = null;
    // A list request sent before the deletion must not bring the cards back.
    state.refreshPending = true;
    renderJobs();
    renderJobsNotice();
    await refresh();
  } catch (error) {
    showJobsNotice({ error: error.message });
  } finally {
    state.deleting = false;
    renderJobs();
    const all = $("selectAllJobs");
    focusHistoryControl(state.selectionMode ? all : $("toggleSelection"));
  }
}
