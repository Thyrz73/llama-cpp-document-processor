// Find an element by its HTML ID, or return null when it is absent.
const $ = (id) => document.getElementById(id);
const catalogNode = $("translations");
const catalog = catalogNode
  ? JSON.parse(catalogNode.textContent)
  : { en: {}, fr: {} };
let uiLanguage = localStorage.getItem("document-processor-language") || "en";
if (!["en", "fr"].includes(uiLanguage)) uiLanguage = "en";

// Interpolate trusted catalogue strings; all rendered values still use textContent.
function t(key, args = {}) {
  let value = catalog[uiLanguage]?.[key] || catalog.en?.[key] || key;
  for (const [name, text] of Object.entries(args))
    value = value.replaceAll("{" + name + "}", String(text));
  return value;
}
// Time out both headers and JSON decoding; a stalled GET must release polling locks.
async function jsonFetch(url, options = {}, timeout = 15000) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(Error(t("Request timed out.")));
    }, timeout);
  });
  try {
    return await Promise.race([
      (async () => {
        const response = await fetch(url, {
          ...options,
          signal: controller.signal,
        });
        const data = await response.json();
        return {
          ok: response.ok,
          status: response.status,
          json: async () => data,
        };
      })(),
      deadline,
    ]);
  } finally {
    clearTimeout(timer);
  }
}
// Prefer a stable translated code over an arbitrary backend error string.
function errorText(data, fallback) {
  const key = "error." + (data.error_code || "generic");
  return catalog[uiLanguage]?.[key] || data.error || t(fallback);
}
// Translate only interface text, leaving the PDF question and model output untouched.
function applyLanguage() {
  document.documentElement.lang = uiLanguage;
  const question = $("question");
  if (
    question &&
    [catalog.en?.default_question, catalog.fr?.default_question].includes(
      question.value,
    )
  )
    question.value = t("default_question");
  for (const node of document.querySelectorAll?.("[data-i18n]") || [])
    node.textContent = t(node.dataset.i18n);
  if ($("uiLanguage")) $("uiLanguage").value = uiLanguage;
  if ($("theme")) $("theme").setAttribute("aria-label", t("Theme"));
  if ($("fileLabel"))
    $("fileLabel").textContent =
      $("file")?.files?.[0]?.name || t("📄 Drop a PDF or click to browse");
  if ($("jobs") && !$("jobs").children.length)
    $("jobs").textContent = t("Loading…");
  if ($("languageFlag"))
    $("languageFlag").src = "/static/" + uiLanguage + ".svg";
  document.title = t("Document Processor · Local PDF analysis");
  localStorage.setItem("document-processor-language", uiLanguage);
}
applyLanguage();

const file = $("file"),
  drop = $("drop"),
  feedback = $("feedback");
let expandedId = null,
  liveTab = "content";
const activeStatuses = new Set(["queued", "running"]);
const cancellingJobs = new Set();
const promptExpanded = new Set();
let jobs = [],
  renderedJobs = null,
  hasActiveJobs = false,
  jobsTimer = null,
  jobsRequest = null,
  refreshPending = false,
  liveBusy = false,
  jobsFailures = 0,
  uploading = false;
const theme = $("theme");
const selectedTheme =
  localStorage.getItem("document-processor-theme") || "system";
theme.value = ["system", "light", "dark"].includes(selectedTheme)
  ? selectedTheme
  : "system";
// Resolve the system mode, apply its color, and remember choice v.
function applyTheme(v) {
  document.documentElement.dataset.theme =
    v === "system"
      ? matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : v;
  localStorage.setItem("document-processor-theme", v);
}
theme.addEventListener("change", () => applyTheme(theme.value));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (theme.value === "system") applyTheme("system");
});
file.addEventListener("change", () => {
  $("fileLabel").textContent =
    file.files[0]?.name || t("📄 Drop a PDF or click to browse");
});
for (const type of ["dragenter", "dragover"])
  drop.addEventListener(type, (e) => {
    e.preventDefault();
    drop.classList.add("active");
  });
for (const type of ["dragleave", "drop"])
  drop.addEventListener(type, (e) => {
    e.preventDefault();
    drop.classList.remove("active");
  });
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) {
    file.files = e.dataTransfer.files;
    $("fileLabel").textContent = file.files[0].name;
  }
});
// Send the PDF and instructions, then display the new job immediately.
// Keep the button disabled until upload and refresh are complete.
$("uploadForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (uploading) return;
  if ($("profile") && !previewValid) {
    feedback.textContent = t("Invalid settings");
    return;
  }
  if (!file.files.length) {
    feedback.textContent = t("Select a PDF.");
    return;
  }
  uploading = true;
  $("send").disabled = true;
  feedback.textContent = t("Uploading file…");
  try {
    const r = await jsonFetch(
      "/api/jobs",
      { method: "POST", body: uploadBody(e.target) },
      120000,
    );
    const data = await r.json();
    if (!r.ok) throw Error(errorText(data, `HTTP ${r.status}`));
    feedback.textContent = t("Analysis added to queue.");
    resetUpload(e.target);
    $("fileLabel").textContent = t("📄 Drop a PDF or click to browse");
    expandedId = data.id;
    liveTab = "content";
    await refresh(true);
  } catch (err) {
    feedback.textContent = t("Failed: ") + err.message;
  } finally {
    uploading = false;
    $("send").disabled = $("profile") && !previewValid;
  }
});
$("refresh").addEventListener("click", refresh);
const codes = {
  queued: "Queued",
  running: "Running",
  done: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};
// Create an element with optional text and an optional CSS class.
// textContent displays plain text without interpreting it as HTML.
function el(tag, content, cls) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (cls) node.className = cls;
  return node;
}
// Requeue job id through the API and force a fresh jobs read.
// Show an alert if the API or network rejects the retry.
async function retry(id) {
  try {
    const r = await jsonFetch(`/api/jobs/${id}/retry`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw Error(errorText(d, "Retry failed"));
    await refresh(true);
  } catch (err) {
    alert(err.message);
  }
}
// Open or close the Live panel for job id without reloading the jobs list.
function toggleLive(id) {
  expandedId = expandedId === id ? null : id;
  liveTab = "content";
  renderJobs();
}
// Build and return the Live panel for job id without starting a request.
// Buttons switch the displayed channel; loadLive supplies the text afterward.
function createLive(id) {
  const root = el("div", undefined, "live");
  root.id = "live-" + id;
  const header = el("div", undefined, "live-head"),
    tabs = el("div", undefined, "tabs");
  for (const tab of ["content", "reasoning"]) {
    const b = el(
      "button",
      tab === "content" ? t("Generated text") : t("Reasoning (if available)"),
      "button-secondary" + (liveTab === tab ? " button-active" : ""),
    );
    b.type = "button";
    b.dataset.tab = tab;
    b.addEventListener("click", () => {
      liveTab = tab;
      root
        .querySelectorAll("[data-tab]")
        .forEach((button) =>
          button.classList.toggle("button-active", button.dataset.tab === tab),
        );
      loadLive();
    });
    tabs.append(b);
  }
  header.append(tabs);
  const passLabel = el("span", t("Latest model pass"), "micro muted");
  passLabel.dataset.i18n = "Latest model pass";
  header.append(passLabel);
  root.append(header);
  const output = el("div", t("Waiting for streamed output…"), "live-output");
  output.id = "live-content";
  output.setAttribute("role", "log");
  root.append(output);
  const note = el(
    "p",
    t(
      "The panel displays the current or most recent model pass. Model reasoning is shown only if emitted by the model.",
    ),
    "live-note",
  );
  note.id = "live-note";
  root.append(note);
  return root;
}
// Read the open panel preview without overlapping Live requests.
// Ignore hidden tabs and responses for a panel replaced in the meantime.
// Preserve manual scrolling; follow output when the user is at the bottom.
async function loadLive() {
  if (document.hidden || liveBusy) return;
  const id = expandedId,
    output = $("live-content"),
    note = $("live-note");
  if (!id || !output || !note) return;
  liveBusy = true;
  try {
    const res = await jsonFetch(
      `/api/jobs/${id}/live` +
        (liveTab === "reasoning" ? "?include_reasoning=true" : ""),
      { cache: "no-store" },
    );
    if (!res.ok) throw Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (id !== expandedId || output !== $("live-content")) return;
    const content = liveTab === "reasoning" ? d.reasoning : d.content;
    const truncated =
      (liveTab === "reasoning" ? d.reasoning_chars : d.content_chars) >
      (content || "").length;
    const label =
      liveTab === "reasoning"
        ? t(
            "No reasoning text was emitted for this pass. The model may not support it, or the server may not expose it.",
          )
        : t("No generated text yet for this pass.");
    const nearBottom =
      output.scrollHeight - output.scrollTop - output.clientHeight < 45;
    if (output.textContent !== (content || label))
      output.textContent = content || label;
    if (nearBottom) output.scrollTop = output.scrollHeight;
    note.textContent =
      phaseLabel(d) +
      " · " +
      t(
        d.active
          ? d.metrics?.first_event_ms == null
            ? "Waiting for model output…"
            : "Generating…"
          : "Last available output",
      ) +
      (truncated ? " · " + t("Showing recent text only") : "") +
      " · " +
      liveMetrics(d);
  } catch (err) {
    if (id === expandedId && output === $("live-content"))
      note.textContent = t("Cannot load live output: ") + err.message;
  } finally {
    liveBusy = false;
    // Load a replacement panel immediately after the previous request settles.
    if ($("live-content") && output !== $("live-content")) loadLive();
  }
}
// Cancel the jobs polling deadline and release its reference when present.
function stopJobsPolling() {
  if (jobsTimer !== null) clearTimeout(jobsTimer);
  jobsTimer = null;
}
// Schedule one read in six seconds when a job is active.
// Do not schedule automatic reads in a hidden tab.
function scheduleJobsPolling() {
  stopJobsPolling();
  if (hasActiveJobs && !document.hidden)
    jobsTimer = setTimeout(
      refresh,
      Math.min(30000, 6000 * 2 ** Math.max(0, jobsFailures - 1)),
    );
}
// Return the jobs-refresh promise shared by callers.
// afterChange=true marks a create/retry: read again after any older response.
// A manual click passes an event, not true. Keep cards visible on errors and
// adapt polling after the request settles.
function refresh(afterChange = false) {
  // A response requested before creation cannot confirm the new job's state.
  if (afterChange === true) {
    hasActiveJobs = true;
    refreshPending = true;
  }
  if (jobsRequest) return jobsRequest;
  stopJobsPolling();
  jobsRequest = (async () => {
    do {
      refreshPending = false;
      try {
        const res = await jsonFetch("/api/jobs", { cache: "no-store" });
        if (!res.ok) throw Error(t("Unable to load jobs"));
        const data = await res.json();
        if (refreshPending) continue;
        jobsFailures = 0;
        jobs = data.jobs;
        hasActiveJobs =
          data.has_active_jobs ??
          jobs.some((j) => activeStatuses.has(j.status));
        $("jobsError").hidden = true;
        renderJobs();
      } catch (err) {
        jobsFailures++;
        const error = $("jobsError"),
          message = t("Cannot refresh analyses: ") + err.message;
        if (error.textContent !== message) error.textContent = message;
        error.hidden = false;
      }
    } while (refreshPending);
  })().finally(() => {
    jobsRequest = null;
    // A creation can complete between the loop end and this cleanup.
    if (refreshPending) return refresh(true);
    scheduleJobsPolling();
  });
  return jobsRequest;
}
// Rebuild cards only when data or the open panel changes.
// Reuse the Live node to preserve its text, channel, and scroll position.
function renderJobs() {
  const signature = JSON.stringify([jobs, expandedId]);
  if (signature === renderedJobs) return;
  const live = expandedId ? $("live-" + expandedId) : null,
    host = $("jobs");
  const priorScroll = live ? $("live-content")?.scrollTop : null;
  host.replaceChildren();
  renderedJobs = signature;
  const groups = {};
  for (const [key, heading, empty] of [
    ["active", "Active analyses", "No analyses in progress."],
    ["history", "Analysis history", "No analysis history yet."],
  ]) {
    const section = el("section", undefined, "job-group job-group-" + key);
    const title = el("h3", t(heading));
    title.id = "jobs-" + key + "-heading";
    section.setAttribute("aria-labelledby", title.id);
    section.append(title);
    const list = el("div", undefined, "job-list");
    section.append(list);
    host.append(section);
    groups[key] = list;
    if (!jobs.some((job) => activeStatuses.has(job.status) === (key === "active")))
      list.append(el("p", t(empty), "muted"));
  }
  for (const j of jobs) {
    const item = el("article", undefined, "item"),
      top = el("div", undefined, "top");
    top.append(el("div", j.filename, "name"));
    top.append(el("span", t(codes[j.status] || j.status), `pill ${j.status}`));
    item.append(top);
    item.append(el("div", phaseLabel(j), "meta"));
    if (j.configuration)
      item.append(
        el(
          "div",
          t("Profile: {profile}", {
            profile: t("profile_name." + j.configuration.profile),
          }) + (j.configuration.customized ? " · " + t("Customized") : ""),
          "meta",
        ),
      );
    item.append(
      el(
        "div",
        t("{pages} pages · {done}/{total} sections · created {date}", {
          pages: j.pages || "?",
          done: j.done_chunks || 0,
          total: j.total_chunks || "?",
          date: new Date(j.created_at).toLocaleString(uiLanguage),
        }),
        "meta",
      ),
    );
    const pct =
      j.status === "done"
        ? 100
        : Math.max(0, Math.min(99, Number(j.progress_percent || 0)));
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
    if (j.error) item.append(el("div", errorText(j, j.error), "error"));
    const actions = el("div", undefined, "actions");
    if (j.status === "done" && j.report_available !== false) {
      item.append(
        el("div", j.report_name || t("Markdown report"), "meta report-name"),
      );
      const view = el("a", t("View"));
      view.href = `/api/jobs/${j.id}/report/view?lang=${uiLanguage}`;
      view.target = "_blank";
      view.rel = "noopener noreferrer";
      const link = el("a", t("Download"));
      link.href = `/api/jobs/${j.id}/report`;
      link.download = j.report_name || "";
      actions.append(view, link);
    }
    if (["failed", "cancelled"].includes(j.status)) {
      const b = el("button", t("Retry"), "button-secondary");
      b.type = "button";
      b.addEventListener("click", () => retry(j.id));
      actions.append(b);
    }
    if (activeStatuses.has(j.status)) {
      const cancel = el(
        "button",
        t(
          j.cancel_requested
            ? "Cancellation requested"
            : cancellingJobs.has(j.id)
              ? "Requesting cancellation…"
              : "Cancel",
        ),
        "button-danger",
      );
      cancel.disabled = !!j.cancel_requested || cancellingJobs.has(j.id);
      cancel.setAttribute("aria-busy", String(cancellingJobs.has(j.id)));
      cancel.type = "button";
      cancel.addEventListener("click", () => cancelJob(j.id));
      actions.append(cancel);
    }
    const stats = el("button", t("Metrics"), "button-secondary");
    stats.type = "button";
    stats.addEventListener("click", () => showMetrics(j.id, item));
    actions.append(stats);
    const liveButton = el(
      "button",
      expandedId === j.id ? t("Hide live output") : t("Show live output"),
      "button-secondary",
    );
    liveButton.type = "button";
    liveButton.setAttribute("aria-expanded", String(expandedId === j.id));
    liveButton.addEventListener("click", () => toggleLive(j.id));
    actions.append(liveButton);
    item.append(actions);
    // The saved request is the only user-configurable prompt; server prompts stay private.
    if (
      j.question && j.question_custom !== 0 &&
      (j.question_custom === 1 || ![
        catalog.en?.default_question, catalog.fr?.default_question,
        "Analyze the entire document",
      ].includes(j.question.trim()))
    ) {
      const details = el("details", undefined, "custom-prompt");
      details.open = promptExpanded.has(j.id);
      details.append(
        el("summary", t(j.question_custom === 1
          ? "Custom analysis instructions"
          : "Saved analysis instructions")),
      );
      details.append(el("pre", j.question, "prompt-text"));
      details.addEventListener("toggle", () => {
        if (details.open) promptExpanded.add(j.id);
        else promptExpanded.delete(j.id);
      });
      item.append(details);
    }
    if (expandedId === j.id) item.append(live || createLive(j.id));
    groups[activeStatuses.has(j.status) ? "active" : "history"].append(item);
  }
  // Browsers may reset scrollTop when a preserved node is detached and reattached.
  if (priorScroll != null && $("live-content"))
    $("live-content").scrollTop = priorScroll;
  loadLive();
}
// When the tab becomes visible, request one jobs and Live refresh.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopJobsPolling();
  else {
    refresh();
    loadLive();
  }
});
// Load the list once; the Live timer is separate from the jobs timer.
refresh();
setInterval(loadLive, 1700);

const overrideNames = [
  "chunk_tokens",
  "analysis_tokens",
  "merge_tokens",
  "final_tokens",
  "temperature",
  "top_p",
  "top_k",
  "min_p",
  "step_timeout_seconds",
  "max_calls",
];
let previewGeneration = 0,
  previewTimer = null,
  previewValid = true,
  profileConfig = null;

// The backend resolves options again; displayed values are not an authoritative snapshot.
function selectedOptions() {
  const overrides = {};
  for (const key of overrideNames) {
    if ($(key)?.value.trim()) overrides[key] = Number($(key).value);
  }
  for (const stage of ["section", "merge", "final"]) {
    const key = "thinking_" + stage;
    if ($(key)?.value && $(key).value !== "inherit")
      overrides[key] = $(key).value;
  }
  return {
    profile: $("profile").value,
    report_language: $("reportLanguage").value,
    detail_level: $("detailLevel").value,
    overrides,
  };
}
function uploadBody(form) {
  const body = new FormData(form);
  if ($("profile")) body.set("options", JSON.stringify(selectedOptions()));
  return body;
}
// Preserve user preferences while clearing only the selected file after a successful upload.
function resetUpload(form) {
  if ($("profile")) {
    file.value = "";
    $("fileLabel").textContent = t("📄 Drop a PDF or click to browse");
  } else form.reset();
}
// Prefer stable backend phase codes; historical English labels remain a fallback.
function phaseLabel(job) {
  const key = "phase." + job.phase_code;
  return catalog[uiLanguage]?.[key]
    ? t(key, job.phase_args || {})
    : job.stage || job.phase || t("Waiting");
}
// Unknown token counts stay unavailable rather than being inferred from text length.
function liveMetrics(data) {
  const m = data.metrics || {},
    parts = [];
  if (data.elapsed_seconds != null)
    parts.push(t("Elapsed: {seconds}s", { seconds: data.elapsed_seconds }));
  if (m.output_tokens != null)
    parts.push(t("Generated: {tokens} tokens", { tokens: m.output_tokens }));
  if (m.decode_tokens_per_second != null)
    parts.push(
      t("Decode: {rate} tokens/s (engine)", {
        rate: m.decode_tokens_per_second.toFixed(2),
      }),
    );
  if (m.output_tokens == null) parts.push(t("Token metrics unavailable"));
  return parts.join(" · ");
}
// Show effective budgets and the base profile without changing input overrides.
function renderConfig(config) {
  profileConfig = config;
  $("configPreview").textContent =
    (config.customized ? t("Customized") + " · " : "") +
    t(
      "Section {chunk} · analysis {analysis} · merge {merge} · final {final} · context limit {context}",
      {
        chunk: config.chunk_tokens,
        analysis: config.analysis_tokens,
        merge: config.merge_tokens,
        final: config.final_tokens,
        context: config.context_tokens,
      },
    );
  $("configPreview").append(
    el(
      "div",
      t("Reasoning: section {section}, merge {merge}, final {final}", {
        section: t(config.thinking_section),
        merge: t(config.thinking_merge),
        final: t(config.thinking_final),
      }),
    ),
  );
  $("configWarnings").textContent = config.warnings
    .map((code) => t("warning." + code))
    .join(" ");
  $("profileHelp").textContent = t("profile." + $("profile").value);
  for (const name of overrideNames)
    if ($(name))
      $(name).placeholder =
        config[name] == null ? t("Engine default") : String(config[name]);
}
// Generation IDs prevent an obsolete preview response from enabling submission.
async function previewConfig() {
  const generation = ++previewGeneration;
  previewValid = false;
  $("send").disabled = true;
  try {
    const response = await jsonFetch("/api/config/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selectedOptions()),
    });
    const data = await response.json();
    if (generation !== previewGeneration) return;
    if (!response.ok) throw Error(errorText(data, "Invalid settings"));
    renderConfig(data.configuration);
    previewValid = true;
  } catch (error) {
    if (generation === previewGeneration) {
      $("configWarnings").textContent = error.message;
      $("configPreview").textContent = t("Invalid settings");
    }
  } finally {
    if (generation === previewGeneration)
      $("send").disabled = uploading || !previewValid;
  }
}
function schedulePreview() {
  clearTimeout(previewTimer);
  previewGeneration++;
  previewValid = false;
  $("send").disabled = true;
  previewTimer = setTimeout(previewConfig, 250);
}
// Capability metadata controls only supported form fields, never the engine.
async function loadProfiles() {
  try {
    const response = await jsonFetch("/api/profiles");
    const data = await response.json();
    if (!response.ok) throw Error(errorText(data, "Invalid settings"));
    for (const stage of ["section", "merge", "final"])
      $("thinking_" + stage).disabled = !data.thinking_supported;
  } catch (error) {
    $("configWarnings").textContent = error.message;
  }
  await previewConfig();
}
// Diagnostics are explicit read-only requests and can refresh preview warnings.
async function inspectBackend() {
  $("diagnostics").disabled = true;
  try {
    const response = await jsonFetch("/api/diagnostics", {}, 25000);
    const data = await response.json();
    if (!response.ok) throw Error(errorText(data, "Backend unavailable"));
    const b = data.backend;
    $("diagnosticOutput").textContent = t(
      "Backend: {status} · model: {model} · context: {context} · slots: {slots}",
      {
        status: t(b.status),
        model: b.model || t("Unknown"),
        context: b.context_tokens ?? t("Unknown"),
        slots: b.slots ?? t("Unknown"),
      },
    );
    $("diagnosticOutput").append(
      el(
        "p",
        t(
          "Processor memory: {memory} KiB. Engine RAM and swap are unavailable.",
          { memory: data.application.process_rss_kib ?? t("Unknown") },
        ),
        "small muted",
      ),
    );
    await previewConfig();
  } catch (error) {
    $("diagnosticOutput").textContent = error.message;
  } finally {
    $("diagnostics").disabled = false;
  }
}
// Cancellation is cooperative; closing the browser itself does not cancel a job.
async function cancelJob(id) {
  const current = jobs.find((job) => job.id === id);
  if (
    !current || !activeStatuses.has(current.status) ||
    current.cancel_requested || cancellingJobs.has(id)
  ) return;
  cancellingJobs.add(id);
  renderJobsForCancellation();
  try {
    const response = await jsonFetch(`/api/jobs/${id}/cancel`, {
      method: "POST",
    });
    const data = await response.json();
    if (!response.ok) throw Error(errorText(data, "Cancellation failed"));
    const job = jobs.find((job) => job.id === id);
    if (job) {
      job.cancel_requested = !!data.cancel_requested;
      job.status = data.status;
    }
    await refresh(true);
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    cancellingJobs.delete(id);
    renderJobsForCancellation();
  }
}
function renderJobsForCancellation() {
  renderedJobs = null;
  renderJobs();
}
// Fetch bounded metrics on demand, outside the frequent progress polling loop.
async function showMetrics(id, item) {
  const previous = $("metrics-" + id);
  if (previous) {
    previous.hidden = !previous.hidden;
    return;
  }
  const output = el("div", t("Loading…"), "metrics");
  output.id = "metrics-" + id;
  item.append(output);
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
            status: t(codes[run.status] || run.status),
            seconds:
              run.duration_ms == null
                ? t("In progress")
                : (run.duration_ms / 1000).toFixed(1),
            queue:
              run.queue_ms == null
                ? t("Unknown")
                : (run.queue_ms / 1000).toFixed(1),
          }),
        ),
      );
    for (const step of [...data.steps].reverse()) {
      const m = step.metrics;
      output.append(
        el(
          "div",
          step.step_key +
            " · " +
            t(step.status) +
            " · " +
            (m.duration_ms == null
              ? "—"
              : (m.duration_ms / 1000).toFixed(1) + "s") +
            " · " +
            t("Generated: {tokens} tokens", {
              tokens: m.output_tokens ?? "—",
            }) +
            " · " +
            (m.finish_reason || m.error_code || "—"),
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
if ($("uiLanguage"))
  $("uiLanguage").addEventListener("change", () => {
    uiLanguage = $("uiLanguage").value;
    applyLanguage();
    if (file.files?.length) $("fileLabel").textContent = file.files[0].name;
    renderedJobs = null;
    const live = expandedId ? $("live-" + expandedId) : null;
    if (live)
      for (const button of live.querySelectorAll("[data-tab]"))
        button.textContent = t(
          button.dataset.tab === "content"
            ? "Generated text"
            : "Reasoning (if available)",
        );
    renderJobs();
    if (profileConfig) renderConfig(profileConfig);
  });
if ($("profile")) {
  for (const name of ["profile", "reportLanguage", "detailLevel"]) {
    const saved = localStorage.getItem("document-processor-" + name);
    if (saved && [...$(name).options].some((option) => option.value === saved))
      $(name).value = saved;
    $(name).addEventListener("change", () => {
      localStorage.setItem("document-processor-" + name, $(name).value);
      schedulePreview();
    });
  }
  for (const name of [
    ...overrideNames,
    "thinking_section",
    "thinking_merge",
    "thinking_final",
  ])
    $(name).addEventListener("input", schedulePreview);
  $("diagnostics").addEventListener("click", inspectBackend);
  loadProfiles();
}
