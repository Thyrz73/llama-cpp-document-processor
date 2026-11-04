// Light, dark or system color theme, remembered by the browser.
// The inline script in templates/index.html applies the saved theme before
// the page is painted; this module handles later changes.
import { $ } from "./dom.js";

const THEME_KEY = "document-processor-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

// Apply theme ("system", "light" or "dark") and remember it.
function applyTheme(theme) {
  document.documentElement.dataset.theme =
    theme === "system"
      ? matchMedia(DARK_QUERY).matches
        ? "dark"
        : "light"
      : theme;
  localStorage.setItem(THEME_KEY, theme);
}

// Show the saved choice in the theme menu and follow later changes,
// including system color changes while "system" is selected.
export function initTheme() {
  const menu = $("theme");
  const saved = localStorage.getItem(THEME_KEY) || "system";
  menu.value = ["system", "light", "dark"].includes(saved) ? saved : "system";
  menu.addEventListener("change", () => applyTheme(menu.value));
  matchMedia(DARK_QUERY).addEventListener("change", () => {
    if (menu.value === "system") applyTheme("system");
  });
}
