// Light or dark color theme, remembered by the browser.
// The inline script in templates/index.html applies the saved theme before
// the page is painted; this module handles later changes.
import { $ } from "./dom.js";
import { getPreference, setPreference } from "./preferences.js";

const THEME_KEY = "document-processor-theme";

// Apply an explicit theme and remember it, matching the early page script.
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  setPreference(THEME_KEY, theme);
}

// Use light for first visits, invalid values and the legacy system setting.
export function initTheme() {
  const menu = $("theme");
  menu.value = getPreference(THEME_KEY) === "dark" ? "dark" : "light";
  // Do not overwrite an unread saved preference with the fallback.
  document.documentElement.dataset.theme = menu.value;
  menu.addEventListener("change", () => applyTheme(menu.value));
}
