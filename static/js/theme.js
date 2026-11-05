// Light or dark color theme, remembered by the browser.
// The inline script in templates/index.html applies the saved theme before
// the page is painted; this module handles later changes.
import { $ } from "./dom.js";

const THEME_KEY = "document-processor-theme";

// Apply an explicit theme and remember it, matching the early page script.
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
}

// Use light for first visits, invalid values and the legacy system setting.
export function initTheme() {
  const menu = $("theme");
  menu.value = localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
  applyTheme(menu.value);
  menu.addEventListener("change", () => applyTheme(menu.value));
}
