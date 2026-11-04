// Resolve the saved theme before the report is painted, without loading app polling.
(function () {
  let preferred = "light";
  try {
    preferred = localStorage.getItem("document-processor-theme") || "light";
  } catch (_) {
    // Keep the light default when browser storage is unavailable.
  }
  document.documentElement.dataset.theme = preferred === "dark" ? "dark" : "light";
})();
