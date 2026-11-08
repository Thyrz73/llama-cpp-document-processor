// Upload form: choosing a PDF (click or drag and drop) and creating a job.
import { $ } from "./dom.js";
import { state } from "./state.js";
import { t, errorText, FILE_PROMPT } from "./i18n.js";
import { jsonFetch } from "./api.js";
import { refresh } from "./jobs.js";
import { selectedOptions } from "./settings.js";

// Connect the file picker, the drop zone and the form.
export function initUpload() {
  const file = $("file"),
    drop = $("drop");
  file.addEventListener("change", () => {
    $("fileLabel").textContent = file.files[0]?.name || t(FILE_PROMPT);
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
  $("uploadForm").addEventListener("submit", submitUpload);
}

// Send the PDF, the request and the settings, then show the new job at once.
// The button stays disabled until the upload and the list reload are done.
async function submitUpload(e) {
  e.preventDefault();
  if (state.uploading) return;
  const feedback = $("feedback");
  if ($("profile") && !state.previewValid) {
    feedback.textContent = t("Invalid settings");
    return;
  }
  if (!$("file").files.length) {
    feedback.textContent = t("Select a PDF.");
    return;
  }
  state.uploading = true;
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
    $("fileLabel").textContent = t(FILE_PROMPT);
    state.expandedId = data.id;
    state.liveTab = "content";
    await refresh(true);
  } catch (err) {
    feedback.textContent = t("Failed: ") + err.message;
  } finally {
    state.uploading = false;
    $("send").disabled = $("profile") && !state.previewValid;
  }
}

// The form fields, plus the chosen settings when the settings panel exists.
function uploadBody(form) {
  const body = new FormData(form);
  if ($("profile")) body.set("options", JSON.stringify(selectedOptions()));
  return body;
}

// After a successful upload, clear only the chosen file: the request and
// the settings stay for the next analysis. A page without the settings
// panel resets the whole form.
function resetUpload(form) {
  if ($("profile")) {
    $("file").value = "";
    $("fileLabel").textContent = t(FILE_PROMPT);
  } else form.reset();
}
