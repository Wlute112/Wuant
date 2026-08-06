const BASE_URL = "http://127.0.0.1:8000";

async function request(path, options) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response had no JSON body; fall back to statusText
    }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  health: () => request("/api/health"),
  getBrokerStatus: () => request("/api/broker/status"),
  configureBroker: (body) =>
    request("/api/broker/config", { method: "POST", body: JSON.stringify(body) }),

  listRuns: (kind) => request(`/api/runs${kind ? `?kind=${kind}` : ""}`),
  getRun: (runId) => request(`/api/runs/${encodeURIComponent(runId)}`),
  deleteRun: (runId) => request(`/api/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }),

  listJobs: () => request("/api/jobs"),
  getJob: (jobId) => request(`/api/jobs/${encodeURIComponent(jobId)}`),
  getJobLogs: (jobId, tailLines = 200) =>
    request(`/api/jobs/${encodeURIComponent(jobId)}/logs?tail_lines=${tailLines}`),
  cancelJob: (jobId) =>
    request(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),

  startBacktest: (body) =>
    request("/api/jobs/backtest", { method: "POST", body: JSON.stringify(body) }),
  startOptimize: (body) =>
    request("/api/jobs/optimize", { method: "POST", body: JSON.stringify(body) }),
  startPaper: (body) =>
    request("/api/jobs/paper", { method: "POST", body: JSON.stringify(body) }),
  startLive: (body) =>
    request("/api/jobs/live", { method: "POST", body: JSON.stringify(body) }),

  getLivePositions: () => request("/api/live/positions"),
  getLiveRisk: () => request("/api/live/risk"),
};
