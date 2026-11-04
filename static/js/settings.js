// Analysis settings: profile, report language, detail level and advanced
// overrides. The server resolves them for the preview shown before
// submission, and again when the job is created.
import { $, el } from "./dom.js";
import { state } from "./state.js";
import { t, errorText } from "./i18n.js";
import { jsonFetch } from "./api.js";

const OVERRIDE_NAMES = [
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
const STAGES = ["section", "merge", "final"];
// Choices remembered by the browser between visits.
const SAVED_CHOICES = ["profile", "reportLanguage", "detailLevel"];
// The preview waits this long after the last change, in milliseconds.
const PREVIEW_DELAY_MS = 250;

// Options sent to the server: the profile, report language and detail level,
// plus only the advanced fields the user filled in.
export function selectedOptions() {
  const overrides = {};
  for (const key of OVERRIDE_NAMES) {
    if ($(key)?.value.trim()) overrides[key] = Number($(key).value);
  }
  for (const stage of STAGES) {
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

// Show the budgets resolved by the server. The inputs keep the user's own
// overrides; the resolved values only appear as placeholders.
export function renderConfig(config) {
  state.profileConfig = config;
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
  for (const name of OVERRIDE_NAMES)
    if ($(name))
      $(name).placeholder =
        config[name] == null ? t("Engine default") : String(config[name]);
}

// Ask the server to resolve the current settings. Each request gets a
// generation number, so an outdated response can never enable submission.
export async function previewConfig() {
  const generation = ++state.previewGeneration;
  state.previewValid = false;
  $("send").disabled = true;
  try {
    const response = await jsonFetch("/api/config/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selectedOptions()),
    });
    const data = await response.json();
    if (generation !== state.previewGeneration) return;
    if (!response.ok) throw Error(errorText(data, "Invalid settings"));
    renderConfig(data.configuration);
    state.previewValid = true;
  } catch (error) {
    if (generation === state.previewGeneration) {
      $("configWarnings").textContent = error.message;
      $("configPreview").textContent = t("Invalid settings");
    }
  } finally {
    if (generation === state.previewGeneration)
      $("send").disabled = state.uploading || !state.previewValid;
  }
}

// Preview the settings once the user pauses; submission waits for it.
export function schedulePreview() {
  clearTimeout(state.previewTimer);
  state.previewGeneration++;
  state.previewValid = false;
  $("send").disabled = true;
  state.previewTimer = setTimeout(previewConfig, PREVIEW_DELAY_MS);
}

// Enable the reasoning controls only when the server supports them, then
// preview the settings.
async function loadProfiles() {
  try {
    const response = await jsonFetch("/api/profiles");
    const data = await response.json();
    if (!response.ok) throw Error(errorText(data, "Invalid settings"));
    for (const stage of STAGES)
      $("thinking_" + stage).disabled = !data.thinking_supported;
  } catch (error) {
    $("configWarnings").textContent = error.message;
  }
  await previewConfig();
}

// Show the backend diagnostics, read only when the user asks; they can
// change the preview warnings, so the preview is refreshed afterwards.
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

// Restore the saved choices, preview each change and load the profiles.
// Pages without the settings panel skip all of this.
export function initSettings() {
  if (!$("profile")) return;
  for (const name of SAVED_CHOICES) {
    const saved = localStorage.getItem("document-processor-" + name);
    if (saved && [...$(name).options].some((option) => option.value === saved))
      $(name).value = saved;
    $(name).addEventListener("change", () => {
      localStorage.setItem("document-processor-" + name, $(name).value);
      schedulePreview();
    });
  }
  for (const name of [...OVERRIDE_NAMES, ...STAGES.map((s) => "thinking_" + s)])
    $(name).addEventListener("input", schedulePreview);
  $("diagnostics").addEventListener("click", inspectBackend);
  loadProfiles();
}
