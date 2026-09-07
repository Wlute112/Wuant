import test from "node:test";
import assert from "node:assert/strict";
import { operationResult, validOperationsSnapshot } from "./operations.js";

test("acknowledgement is never presented as broker confirmation", () => {
  assert.match(operationResult({ status: "ACKNOWLEDGED" }), /confirmation pending/);
  assert.match(operationResult({ status: "COMPLETED", result: { broker_flat_confirmed: true } }), /Broker confirmed/);
  assert.match(operationResult({ status: "FAILED", result: { blockers: ["Uncertain broker", "Kill engaged"] } }), /Uncertain broker Kill engaged/);
  assert.match(operationResult({ status: "CANCELLED" }), /before the strategy claimed/);
});

test("missing, malformed and different-session snapshots cannot unlock controls", () => {
  const state = { job_id: "paper_1", heartbeat_fresh: true, resume_blockers: [], commands: [] };
  assert.equal(validOperationsSnapshot(state, "paper_1"), true);
  for (const bad of [null, { ...state, job_id: "paper_2" }, { ...state, resume_blockers: null },
    { ...state, commands: [null] }, { ...state, heartbeat_fresh: "true" }]) {
    assert.equal(validOperationsSnapshot(bad, "paper_1"), false);
  }
});
