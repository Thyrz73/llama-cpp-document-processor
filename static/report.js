// Resolve the saved theme before the report is painted, without loading app polling.
(function () {
  let preferred = "system";
  try {
    preferred = localStorage.getItem("document-processor-theme") || "system";
  } catch (_) {
    // The system theme still works when browser storage is unavailable.
  }
  const system = matchMedia("(prefers-color-scheme: dark)");
  function applyTheme() {
    document.documentElement.dataset.theme = ["light", "dark"].includes(preferred)
      ? preferred
      : system.matches ? "dark" : "light";
  }
  applyTheme();
  system.addEventListener("change", applyTheme);
})();
