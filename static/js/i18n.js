// Interface translations: the catalog embedded in the page, the chosen
// language, and the labels of job statuses and phases.
import { $ } from "./dom.js";
import { state } from "./state.js";

const LANGUAGE_KEY = "document-processor-language";
const STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  done: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};
// Text of the file picker while no PDF is selected.
export const FILE_PROMPT = "📄 Drop a PDF or click to browse";

// Read the catalog from the page and the language saved by the browser.
export function loadTranslations() {
  const node = $("translations");
  state.catalog = node ? JSON.parse(node.textContent) : { en: {}, fr: {} };
  const saved = localStorage.getItem(LANGUAGE_KEY) || "en";
  state.language = ["en", "fr"].includes(saved) ? saved : "en";
}

// Translate key, replacing each {name} placeholder with args[name].
// Callers display the result with textContent, never as HTML.
export function t(key, args = {}) {
  const { catalog, language } = state;
  let value = catalog[language]?.[key] || catalog.en?.[key] || key;
  for (const [name, text] of Object.entries(args))
    value = value.replaceAll("{" + name + "}", String(text));
  return value;
}

// Prefer the translation of a stable error code over the server's message.
export function errorText(data, fallback) {
  const key = "error." + (data.error_code || "generic");
  return state.catalog[state.language]?.[key] || data.error || t(fallback);
}

// Translated label of a job or attempt status.
export function statusLabel(status) {
  return t(STATUS_LABELS[status] || status);
}

// Translated phase of a job. Without a translation for its phase code, the
// job's stored English stage is shown.
export function phaseLabel(job) {
  const key = "phase." + job.phase_code;
  return state.catalog[state.language]?.[key]
    ? t(key, job.phase_args || {})
    : job.stage || job.phase || t("Waiting");
}

// Translate the page's static text and remember the language.
// The user's request and the model output are never translated.
export function applyLanguage() {
  const { catalog, language } = state;
  document.documentElement.lang = language;
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
  if ($("uiLanguage")) $("uiLanguage").value = language;
  if ($("theme")) $("theme").setAttribute("aria-label", t("Theme"));
  if ($("fileLabel"))
    $("fileLabel").textContent = $("file")?.files?.[0]?.name || t(FILE_PROMPT);
  if ($("jobs") && !$("jobs").children.length)
    $("jobs").textContent = t("Loading…");
  if ($("languageFlag")) $("languageFlag").src = "/static/" + language + ".svg";
  document.title = t("Document Processor · Local PDF analysis");
  localStorage.setItem(LANGUAGE_KEY, language);
}
