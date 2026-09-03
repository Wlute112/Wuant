import test from "node:test";
import assert from "node:assert/strict";

import {
  activeRootJobCount,
  executionJobFor,
  isJobActive,
  orderedJobRows,
  supervisorFor,
} from "./jobs.js";

const jobs = [
  {
    id: "paper_new",
    kind: "paper",
    status: "starting",
    config: { asset_class: "equity" },
    companion_job_ids: ["risk_new"],
  },
  {
    id: "risk_new",
    kind: "risk_supervisor",
    status: "running",
    parent_job_id: "paper_new",
  },
  {
    id: "paper_old",
    kind: "paper",
    status: "cancelled",
    config: { asset_class: "equity" },
  },
];

test("starting and cancelling remain active lifecycle states", () => {
  assert.equal(isJobActive({ status: "starting" }), true);
  assert.equal(isJobActive({ status: "cancelling" }), true);
  assert.equal(isJobActive({ status: "cancelled" }), false);
});

test("companion processes do not inflate the active workflow count", () => {
  assert.equal(activeRootJobCount(jobs), 1);
});

test("supervisors render directly beneath their execution session", () => {
  assert.deepEqual(
    orderedJobRows(jobs).map(({ job, depth }) => [job.id, depth]),
    [["paper_new", 0], ["risk_new", 1], ["paper_old", 0]],
  );
});

test("active execution and linked supervisor resolve from durable job records", () => {
  const execution = executionJobFor(jobs, "paper", "equity");
  assert.equal(execution.id, "paper_new");
  assert.equal(supervisorFor(jobs, execution).id, "risk_new");
});
