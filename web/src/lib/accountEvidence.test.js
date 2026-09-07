import test from "node:test";
import assert from "node:assert/strict";
import { settledCashLabel } from "./accountEvidence.js";

const evidence = { status: "CURRENT", currency: "USD", value: "0", observed_at: 1000 };
const options = { connected: true, now: 1001000 };

test("cash displays zero and negative broker balances", () => {
  assert.equal(settledCashLabel(evidence, options), "$0.00 · USD settled");
  assert.equal(settledCashLabel({ ...evidence, value: "-12.34" }, options), "-$12.34 · USD settled");
});
test("old responses expire and missing, disconnected, invalid evidence cannot look current", () => {
  assert.match(settledCashLabel(evidence), /disconnected/);
  assert.match(settledCashLabel(null, options), /UNAVAILABLE/);
  assert.match(settledCashLabel(evidence, { ...options, now: 1241000 }), /STALE/);
  for (const value of [null, true, "", "NaN", "Infinity"]) {
    assert.match(settledCashLabel({ ...evidence, value }, options), /INVALID/);
  }
  assert.match(settledCashLabel({ ...evidence, observed_at: 2000 }, options), /INVALID/);
  assert.match(settledCashLabel({ ...evidence, currency: "EUR" }, options), /UNAVAILABLE/);
  assert.match(settledCashLabel({ ...evidence, status: "STALE" }, options), /STALE/);
});
