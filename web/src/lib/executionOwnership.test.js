import test from "node:test";
import assert from "node:assert/strict";
import { ownershipIsFresh } from "./executionOwnership.js";

test("ownership expires when local polling or the server observation stops", () => {
  const now = Date.parse("2026-10-20T15:00:00Z");
  const report = { as_of: new Date(now).toISOString() };
  assert.equal(ownershipIsFresh(report, now, now), true);
  assert.equal(ownershipIsFresh(report, now, now + 10001), false);
  assert.equal(ownershipIsFresh({ as_of: new Date(now - 10001).toISOString() }, now, now), false);
});

test("unknown time and clock jumps cannot establish current ownership", () => {
  const now = Date.parse("2026-10-20T15:00:00Z");
  assert.equal(ownershipIsFresh({}, now, now), false);
  assert.equal(ownershipIsFresh({ as_of: "invalid" }, now, now), false);
  assert.equal(ownershipIsFresh({ as_of: new Date(now + 5001).toISOString() }, now, now), false);
  assert.equal(ownershipIsFresh({ as_of: new Date(now).toISOString() }, now + 1, now), false);
});
