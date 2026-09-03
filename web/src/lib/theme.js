export const DASHBOARD_THEMES = [
  { id: "dark", label: "Dark" },
  { id: "light", label: "White" },
  { id: "lilac", label: "Lilac" },
];

export const DEFAULT_DASHBOARD_THEME = "dark";
export const DASHBOARD_THEME_STORAGE_KEY = "quant-dashboard.theme.v1";

const THEME_IDS = new Set(DASHBOARD_THEMES.map(({ id }) => id));

export function normalizeDashboardTheme(value) {
  return THEME_IDS.has(value) ? value : DEFAULT_DASHBOARD_THEME;
}

export function initialDashboardTheme() {
  const documentTheme = document.documentElement.dataset.theme;
  if (THEME_IDS.has(documentTheme)) return documentTheme;
  try {
    return normalizeDashboardTheme(window.localStorage.getItem(DASHBOARD_THEME_STORAGE_KEY));
  } catch {
    return DEFAULT_DASHBOARD_THEME;
  }
}

export function applyDashboardTheme(value) {
  const theme = normalizeDashboardTheme(value);
  document.documentElement.dataset.theme = theme;
  try {
    window.localStorage.setItem(DASHBOARD_THEME_STORAGE_KEY, theme);
  } catch {
    // Theme selection still applies when browser storage is unavailable.
  }

  const background = window.getComputedStyle(document.documentElement)
    .getPropertyValue("--color-void")
    .trim();
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", background);
  return theme;
}
