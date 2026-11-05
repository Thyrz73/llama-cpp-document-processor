// Mutable state of the interface, shared by all modules.
// Every value that changes lives in this one object: modules keep no state
// of their own, so start() can reset the whole interface with resetState().

// Job statuses that can still change, and those that are final.
export const ACTIVE_STATUSES = new Set(["queued", "running"]);
export const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

export const state = initialState();

// Give every field its value from when the page opens.
export function resetState() {
  for (const timer of [state.jobsTimer, state.liveTimer, state.previewTimer])
    if (timer !== null) clearTimeout(timer);
  state.uiCleanup?.();
  const generation = state.uiGeneration + 1;
  Object.assign(state, initialState(), { uiGeneration: generation });
}

function initialState() {
  return {
    uiActive: true,
    uiGeneration: 0,
    uiCleanup: null,
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
    // Outcome of the last retry, cancellation or deletion, shown above the list.
    jobsNotice: null,
    // Live panel (live.js).
    expandedId: null,
    liveTab: "content",
    liveBusy: false,
    liveTimer: null,
    liveContext: null,
    // History selection and deletion (history.js).
    selectionMode: false,
    selectedJobs: new Set(),
    deleting: false,
    deleteIntent: null,
    deleteTrigger: null,
    // Upload form and settings preview (upload.js, settings.js).
    uploading: false,
    previewGeneration: 0,
    previewTimer: null,
    previewValid: true,
    profileConfig: null,
  };
}
