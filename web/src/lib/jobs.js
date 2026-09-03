export const ACTIVE_JOB_STATUSES = new Set(["starting", "running", "cancelling"]);

const STATUS_LABELS = {
  starting: "Starting",
  running: "Running",
  cancelling: "Stopping",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const KIND_LABELS = {
  backtest: "Backtest",
  optimize: "Optuna sweep",
  paper: "Paper session",
  live: "Live session",
  risk_supervisor: "Risk supervisor",
};

export function isJobActive(job) {
  return ACTIVE_JOB_STATUSES.has(job?.status);
}

export function jobStatusLabel(status) {
  const normalized = String(status || "unknown").toLowerCase();
  return STATUS_LABELS[normalized] || normalized.replaceAll("_", " ");
}

export function jobKindLabel(kind) {
  const normalized = String(kind || "job").toLowerCase();
  return KIND_LABELS[normalized] || normalized.replaceAll("_", " ");
}

export function orderedJobRows(jobs = []) {
  const childrenByParent = new Map();
  for (const job of jobs) {
    if (!job.parent_job_id) continue;
    const children = childrenByParent.get(job.parent_job_id) || [];
    children.push(job);
    childrenByParent.set(job.parent_job_id, children);
  }

  const rows = [];
  const seen = new Set();
  for (const job of jobs) {
    if (job.parent_job_id) continue;
    rows.push({ job, depth: 0 });
    seen.add(job.id);
    for (const child of childrenByParent.get(job.id) || []) {
      rows.push({ job: child, depth: 1 });
      seen.add(child.id);
    }
  }
  for (const job of jobs) {
    if (!seen.has(job.id)) rows.push({ job, depth: job.parent_job_id ? 1 : 0 });
  }
  return rows;
}

export function activeRootJobCount(jobs = []) {
  return jobs.filter((job) => !job.parent_job_id && isJobActive(job)).length;
}

export function executionJobFor(jobs = [], mode, assetClass, preferredId = null) {
  if (preferredId) {
    const preferred = jobs.find((job) => job.id === preferredId);
    if (preferred) return preferred;
  }
  const matching = jobs.filter(
    (job) => job.kind === mode
      && !job.parent_job_id
      && (job.config?.asset_class || "crypto") === assetClass,
  );
  return matching.find(isJobActive) || matching[0] || null;
}

export function supervisorFor(jobs = [], executionJob = null) {
  if (!executionJob) return null;
  const linkedIds = new Set([
    ...(executionJob.companion_job_ids || []),
    executionJob.supervisor_job_id,
  ].filter(Boolean));
  return jobs.find(
    (job) => job.kind === "risk_supervisor"
      && (job.parent_job_id === executionJob.id || linkedIds.has(job.id)),
  ) || null;
}
