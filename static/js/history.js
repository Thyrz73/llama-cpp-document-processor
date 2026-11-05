// History: selection of finished analyses and the deletion confirmation dialog.
// The deletion request and the notice reporting its outcome are in jobs.js,
// which reloads the list.
import { $, el } from "./dom.js";
import { state, TERMINAL_STATUSES } from "./state.js";
import { t } from "./i18n.js";

// The list owns rendering; a callback keeps this module independent of jobs.js.
export function historyHeading(title, render) {
  const row = el("div", undefined, "top history-heading");
  const toggle = el("button", t(state.selectionMode ? "Cancel" : "Select"), "button-secondary");
  toggle.id = "toggleSelection";
  toggle.type = "button";
  toggle.disabled = state.deleting;
  toggle.setAttribute("aria-pressed", String(state.selectionMode));
  toggle.setAttribute("aria-controls", "historyControls");
  toggle.addEventListener("click", () => {
    if (state.deleting) return;
    state.selectionMode = !state.selectionMode;
    state.selectedJobs.clear();
    render();
    $("toggleSelection")?.focus();
  });
  row.append(title, toggle);
  return row;
}

// Build separate controls above the cards and bulk actions below the list.
// eligible holds the IDs of the finished analyses shown in the list.
export function historyControls(eligible) {
  const controls = el("div", undefined, "history-controls");
  controls.id = "historyControls";
  const label = el("label", undefined, "selection-label");
  const all = el("input");
  all.type = "checkbox";
  all.id = "selectAllJobs";
  all.addEventListener("change", () => {
    if (!state.selectionMode || state.deleting) {
      updateSelection();
      return;
    }
    for (const id of eligible) {
      if (all.checked) state.selectedJobs.add(id);
      else state.selectedJobs.delete(id);
    }
    updateSelection();
  });
  label.append(all, el("span", t("Select all displayed analyses")));
  controls.append(label);
  const summary = el("span", undefined, "small muted");
  summary.id = "selectionSummary";
  controls.append(summary);
  const bar = el("div", undefined, "selection-actions");
  bar.id = "selectionActions";
  const count = el("span");
  count.id = "selectionCount";
  count.setAttribute("role", "status");
  const clear = el("button", t("Deselect"), "button-secondary");
  clear.id = "clearSelection";
  clear.type = "button";
  clear.addEventListener("click", () => {
    if (state.deleting) return;
    state.selectedJobs.clear();
    updateSelection();
    focusHistoryControl($("selectAllJobs"));
  });
  const remove = el("button", t("Delete selection"), "button-danger");
  remove.id = "deleteSelection";
  remove.type = "button";
  remove.addEventListener("click", () =>
    confirmDeletion([...state.selectedJobs], true, remove),
  );
  bar.append(count, clear, remove);
  return { controls, actions: bar };
}

// Show the selection on the checkboxes, cards and toolbar.
// Only the finished analyses of the displayed list can be selected.
export function updateSelection() {
  const { selectedJobs, deleting, selectionMode } = state;
  const eligible = state.jobs.filter((job) => TERMINAL_STATUSES.has(job.status));
  const all = $("selectAllJobs");
  if (!all) return;
  all.checked = eligible.length > 0 && selectedJobs.size === eligible.length;
  all.indeterminate =
    selectedJobs.size > 0 && selectedJobs.size < eligible.length;
  all.disabled = deleting || !eligible.length;
  for (const checkbox of $("jobs").querySelectorAll("[data-select-job]")) {
    checkbox.hidden = !selectionMode;
    checkbox.checked = selectedJobs.has(checkbox.dataset.selectJob);
    checkbox.disabled = deleting;
  }
  for (const item of $("jobs").querySelectorAll("[data-job-id]"))
    item.classList.toggle("item-selected", selectedJobs.has(item.dataset.jobId));
  $("historyControls").hidden = !selectionMode;
  $("selectionActions").hidden = !selectionMode || !selectedJobs.size;
  $("selectionCount").textContent = t("{count} analyses selected", {
    count: selectedJobs.size,
  });
  $("selectionSummary").textContent = $("selectionCount").textContent;
  $("clearSelection").disabled = deleting;
  $("deleteSelection").disabled = deleting;
  $("deleteSelection").textContent = t(
    deleting ? "Deleting…" : "Delete ({count})", { count: selectedJobs.size },
  );
  $("deleteSelection").setAttribute("aria-busy", String(deleting));
}

// Fill the confirmation dialog for the pending deletion.
export function renderDeleteDialog() {
  const intent = state.deleteIntent;
  if (!intent) return;
  $("deleteTitle").textContent = t(
    intent.bulk ? "Delete selected analyses?" : "Delete analysis?",
  );
  $("deleteMessage").textContent = t(
    intent.bulk
      ? "Permanently delete {count} analyses? This action cannot be undone."
      : "Delete this analysis from history? This action cannot be undone.",
    { count: intent.ids.length },
  );
  $("deleteDocument").textContent = intent.bulk ? "" : intent.name;
}

// Ask for confirmation before deleting ids (finished analyses only).
// trigger is the button that opened the dialog; it gets the focus back.
export function confirmDeletion(ids, bulk, trigger) {
  if (state.deleting || state.deleteIntent) return;
  const eligible = ids.filter((id) =>
    state.jobs.some(
      (job) => job.id === id && TERMINAL_STATUSES.has(job.status),
    ),
  );
  if (!eligible.length) return;
  state.deleteIntent = {
    ids: [...new Set(eligible)],
    bulk,
    name: state.jobs.find((job) => job.id === eligible[0]).filename,
  };
  state.deleteTrigger = trigger;
  renderDeleteDialog();
  $("deleteDialog").showModal();
  $("deleteCancel").focus();
}

// Close the dialog without deleting anything.
export function closeDeleteDialog() {
  state.deleteIntent = null;
  $("deleteDialog").close();
  const trigger = state.deleteTrigger;
  const replacement = trigger?.dataset?.deleteJob
    ? [...$("jobs").querySelectorAll("[data-delete-job]")].find(
        (node) => node.dataset.deleteJob === trigger.dataset.deleteJob,
      )
    : trigger?.id ? $(trigger.id) : null;
  const target = trigger?.isConnected ? trigger : replacement;
  focusHistoryControl(target);
}

// A refresh may remove the initiator or hide its action bar while a dialog is open.
export function focusHistoryControl(target) {
  (target && !target.disabled && !target.closest("[hidden]")
    ? target : $("toggleSelection"))?.focus();
}
