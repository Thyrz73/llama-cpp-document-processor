// Mutable state of the interface, shared by all modules.
// Every value that changes lives in this one object: modules keep no state
// of their own, so start() can reset the whole interface with resetState().

// Job statuses that can still change, and those that are final.
export const ACTIVE_STATUSES = new Set(["queued", "running"]);
export const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

export const state = initialState();

// Give every field its value from when the page opens.
export function resetState() {
  Object.assign(state, initialState());
}

function initialState() {
  return {
    // Interface language and translation catalog (i18n.js).
    language: "en",
    catalog: { en: {}, fr: {} },
    // Analyses list and its polling (jobs.js).
    jobs: [],
    hasActiveJobs: false,
    renderedSignature: null,
    jobsTimer: null,
    jobsRequest: null,
    refreshPending: false,
    jobsFailures: 0,
    cancellingJobs: new Set(),
    promptExpanded: new Set(),
    // Live panel (live.js).
    expandedId: null,
    liveTab: "content",
    liveBusy: false,
    // History selection and deletion (history.js).
    selectedJobs: new Set(),
    deleting: false,
    deleteIntent: null,
    deleteTrigger: null,
    historyNotice: null,
    // Upload form and settings preview (upload.js, settings.js).
    uploading: false,
    previewGeneration: 0,
    previewTimer: null,
    previewValid: true,
    profileConfig: null,
  };
}
