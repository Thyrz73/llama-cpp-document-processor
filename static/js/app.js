// Start the interface: reset the state, translate the page, connect the
// controls to their modules and load the analyses.
// main.js calls start() once per page; the tests call it for every
// simulated page, which is why it begins with a fresh state.
import { $ } from "./dom.js";
import { state, resetState } from "./state.js";
import { applyLanguage, loadTranslations } from "./i18n.js";
import { initTheme } from "./theme.js";
import { initUpload } from "./upload.js";
import {
  refresh,
  renderJobsNotice,
  rerenderJobs,
  stopJobsPolling,
  submitDeletion,
} from "./jobs.js";
import { stopLivePolling, syncLivePolling, translateLive } from "./live.js";
import { closeDeleteDialog, renderDeleteDialog } from "./history.js";
import { initSettings, renderConfig } from "./settings.js";

export function start() {
  resetState();
  loadTranslations();
  applyLanguage();
  initTheme();
  initUpload();
  $("refresh").addEventListener("click", refresh);
  if ($("deleteDialog")) {
    $("deleteCancel").addEventListener("click", closeDeleteDialog);
    // Escape closes the dialog like the Cancel button: nothing is deleted.
    $("deleteDialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      closeDeleteDialog();
    });
    $("deleteConfirm").addEventListener("click", submitDeletion);
  }
  // A hidden tab stops polling; it reloads once when it becomes visible again.
  const page = document;
  const visibility = () => {
    if (page.hidden) {
      stopJobsPolling();
      stopLivePolling();
    } else {
      refresh();
      syncLivePolling(true);
    }
  };
  page.addEventListener("visibilitychange", visibility);
  state.uiCleanup = () => {
    page.removeEventListener("visibilitychange", visibility);
  };
  refresh();
  $("uiLanguage")?.addEventListener("change", changeLanguage);
  initSettings();
}

// Release polling when the page is destroyed; late requests cannot restart it.
export function stop() {
  stopJobsPolling();
  stopLivePolling();
  state.uiCleanup?.();
  state.uiCleanup = null;
  state.uiActive = false;
  state.uiGeneration++;
}

// Switch the interface language; reports and model output are unchanged.
function changeLanguage() {
  state.language = $("uiLanguage").value;
  applyLanguage();
  translateLive();
  rerenderJobs();
  renderDeleteDialog();
  renderJobsNotice();
  if (state.profileConfig) renderConfig(state.profileConfig);
}
