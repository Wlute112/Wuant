import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_SESSION_POLICY, sessionPolicyPayload } from "./sessionPolicy.js";

test("defaults follow data session and keep cancellation enabled", () => {
  assert.equal(sessionPolicyPayload(DEFAULT_SESSION_POLICY, false).mode, "RTH_ONLY");
  assert.equal(sessionPolicyPayload(DEFAULT_SESSION_POLICY, true).mode, "EXTENDED_HOURS");
  assert.equal(sessionPolicyPayload(DEFAULT_SESSION_POLICY, true).cancel_entries_at_session_end, true);
});
test("custom windows preserve breaks and explicit risk session assignment", () => {
  const payload = sessionPolicyPayload({ ...DEFAULT_SESSION_POLICY, mode: "CUSTOM",
    windows: "09:30-12:00,13:00-16:00", overnight_pnl_assignment: "PRIOR_SESSION" }, true);
  assert.deepEqual(payload.custom_windows, [["09:30", "12:00"], ["13:00", "16:00"]]);
  assert.equal(payload.overnight_pnl_assignment, "PRIOR_SESSION");
});
test("invalid inputs and unsupported routing are rejected before submission", () => {
  for (const windows of ["", "25:00-16:00", "09:30", "09:30-09:30"]) {
    assert.throws(() => sessionPolicyPayload({ ...DEFAULT_SESSION_POLICY, mode: "CUSTOM", windows }, true));
  }
  assert.throws(() => sessionPolicyPayload({ ...DEFAULT_SESSION_POLICY, mode: "CUSTOM" }, false), /extended hours/);
  for (const minutes of ["", -1, 1.5, "foo", 1441]) {
    assert.throws(() => sessionPolicyPayload({ ...DEFAULT_SESSION_POLICY, opening_buffer_minutes: minutes }, false), /whole minutes/);
  }
});
