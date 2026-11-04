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
  rerenderJobs,
  stopJobsPolling,
  submitDeletion,
} from "./jobs.js";
import { loadLive, translateLive } from "./live.js";
import {
  closeDeleteDialog,
  renderDeleteDialog,
  renderHistoryNotice,
} from "./history.js";
import { initSettings, renderConfig } from "./settings.js";

// The open live panel is reloaded this often, in milliseconds.
const LIVE_REFRESH_MS = 1700;

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
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopJobsPolling();
    else {
      refresh();
      loadLive();
    }
  });
  refresh();
  setInterval(loadLive, LIVE_REFRESH_MS);
  $("uiLanguage")?.addEventListener("change", changeLanguage);
  initSettings();
}

// Switch the interface language; reports and model output are unchanged.
function changeLanguage() {
  state.language = $("uiLanguage").value;
  applyLanguage();
  translateLive();
  rerenderJobs();
  renderDeleteDialog();
  renderHistoryNotice();
  if (state.profileConfig) renderConfig(state.profileConfig);
}
