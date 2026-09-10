import test from "node:test";
import assert from "node:assert/strict";
import { executionEvidenceCurrent, executionParamsWithRisk, sectorEvidenceStatus, telemetryIsFresh } from "./sectorRisk.js";

test("explicit dashboard risk settings override nested optimizer settings without mutating uploads", () => {
  for (const key of ["params", "best_params"]) {
    const uploaded = { [key]: { max_sector_exposure_pct: 0.9, n_lags: 7 }, ibkr_bar_hours: 4 };
    const result = executionParamsWithRisk(uploaded, { max_sector_exposure_pct: 0.2 });
    assert.equal(result[key].max_sector_exposure_pct, 0.2);
    assert.equal(result.max_sector_exposure_pct, 0.2);
    assert.equal(result[key].n_lags, 7);
    assert.equal(result.ibkr_bar_hours, 4);
    assert.equal(uploaded[key].max_sector_exposure_pct, 0.9);
  }
});

test("successful polling cannot make an old or future telemetry snapshot current", () => {
  const now = Date.parse("2026-09-09T00:00:30Z");
  assert.equal(telemetryIsFresh("2026-09-09T00:00:10Z", now), true);
  for (const value of [undefined, "invalid", "2026-09-09T00:00:09Z", "2026-09-09T00:00:31Z"]) {
    assert.equal(telemetryIsFresh(value, now), false);
  }
});

test("malformed persisted evidence has an actionable status", () => {
  assert.match(sectorEvidenceStatus({ classifications: [null] }), /INVALID/);
  assert.match(sectorEvidenceStatus({ classifications: {} }), /MISSING/);
});

test("sector upload status exposes missing symbols and expiry", () => {
  const now = Date.parse("2026-09-09T00:00:00Z");
  const evidence = { classifications: [{ symbol: "AAA", valid_until: "2026-09-10T00:00:00Z" }] };
  assert.match(sectorEvidenceStatus(null, ["AAA"], now), /MISSING/);
  assert.match(sectorEvidenceStatus(evidence, ["BBB"], now), /MISSING · BBB/);
  assert.match(sectorEvidenceStatus(evidence, ["AAA"], now), /LOADED/);
  assert.match(sectorEvidenceStatus(evidence, ["AAA"], now + 86400000), /EXPIRED/);
});

test("execution evidence never appears current for demo stale failed or disconnected sessions", () => {
  const state = { executionJob: { status: "running" }, risk: { broker_connectivity: { healthy: true, status: "RECONCILED" } } };
  assert.equal(executionEvidenceCurrent(state), true);
  for (const override of [{ isDemo: true }, { statusUnknown: true }, { feedError: "offline" },
    { executionJob: { status: "cancelled" } }, { risk: {} },
    { risk: { broker_connectivity: { healthy: false, status: "RECONCILIATION_REQUIRED" } } }]) {
    assert.equal(executionEvidenceCurrent({ ...state, ...override }), false);
  }
});
