import assert from "node:assert/strict";
import test from "node:test";

import {
  DASHBOARD_THEMES,
  DEFAULT_DASHBOARD_THEME,
  normalizeDashboardTheme,
} from "./theme.js";

test("dashboard themes expose the three supported defaults", () => {
  assert.deepEqual(
    DASHBOARD_THEMES.map(({ id, label }) => [id, label]),
    [["dark", "Dark"], ["light", "White"], ["lilac", "Lilac"]],
  );
});

test("unknown dashboard themes fail closed to dark", () => {
  assert.equal(normalizeDashboardTheme("light"), "light");
  assert.equal(normalizeDashboardTheme("lilac"), "lilac");
  assert.equal(normalizeDashboardTheme("sepia"), DEFAULT_DASHBOARD_THEME);
  assert.equal(normalizeDashboardTheme(null), DEFAULT_DASHBOARD_THEME);
});
