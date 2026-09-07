import test from "node:test";
import assert from "node:assert/strict";
import { killSwitchStatus } from "./riskStatus.js";

test("clear requires an explicit fresh boolean from real telemetry", () => {
  for (const value of [undefined, null, 0, "false", {}]) {
    assert.equal(killSwitchStatus({ kill_switch_engaged: value }), "UNKNOWN");
  }
  assert.equal(killSwitchStatus(null), "UNKNOWN");
  assert.equal(killSwitchStatus({ kill_switch_engaged: false }), "CLEAR");
  assert.equal(killSwitchStatus({ kill_switch_engaged: true }), "ENGAGED");
  assert.equal(killSwitchStatus({ kill_switch_engaged: false }, { statusUnknown: true }), "UNKNOWN");
  assert.equal(killSwitchStatus({ kill_switch_engaged: false }, { isDemo: true }), "DEMO");
});
