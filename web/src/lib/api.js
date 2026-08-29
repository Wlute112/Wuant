const BASE_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

async function request(path, options) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    let errorDetail = null;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (body.detail && typeof body.detail.message === "string") {
        detail = body.detail.message;
      } else if (Array.isArray(body.detail) && body.detail[0]?.msg) {
        detail = body.detail[0].msg;
      }
      if (body.detail && typeof body.detail === "object" && !Array.isArray(body.detail)) {
        errorDetail = body.detail;
      }
    } catch {
      // response had no JSON body; fall back to statusText
    }
    const error = new Error(detail);
    error.status = res.status;
    error.path = path;
    if (errorDetail) error.detail = errorDetail;
    throw error;
  }
  return res.json();
}

export const api = {
  health: () => request("/api/health"),
  getBrokerStatus: () => request("/api/broker/status"),
  configureBroker: (body) =>
    request("/api/broker/config", { method: "POST", body: JSON.stringify(body) }),
  subscribeBrokerBars: (body) =>
    request("/api/broker/bars/subscribe", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getBrokerBars: (
    symbol,
    assetClass = "equity",
    barHours = 1,
    includeExtendedHours = false,
  ) => {
    const query = new URLSearchParams({
      symbol,
      asset_class: assetClass,
      bar_hours: String(barHours),
      include_extended_hours: String(includeExtendedHours),
    });
    return request(`/api/broker/bars?${query}`);
  },
  getProfiles: () => request("/api/profiles"),
  getLiveReadiness: () => request("/api/readiness/live"),

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

  getLiveTelemetry: (assetClass = "crypto", jobId = null, mode = "paper") => {
    const query = new URLSearchParams({ asset_class: assetClass, mode });
    if (jobId) query.set("job_id", jobId);
    return request(`/api/live/telemetry?${query}`);
  },
  getLivePositions: (assetClass = "crypto", jobId = null, mode = "paper") => {
    const query = new URLSearchParams({ asset_class: assetClass, mode });
    if (jobId) query.set("job_id", jobId);
    return request(`/api/live/positions?${query}`);
  },
  getLiveRisk: (assetClass = "crypto", jobId = null, mode = "paper") => {
    const query = new URLSearchParams({ asset_class: assetClass, mode });
    if (jobId) query.set("job_id", jobId);
    return request(`/api/live/risk?${query}`);
  },
  getLiveNews: ({
    tickers = [],
    jobId = null,
    newsRawScale = 0.001,
    newsScoreClip = 1,
    newsSource = "raw",
    factorEnabled = false,
    factorAsOf = null,
    halfLifeHours = 12,
    maxAgeHours = 72,
    directWeight = 1,
    industryWeight = 0.45,
    commodityWeight = 0.55,
    macroWeight = 0.2,
    limit = 80,
  } = {}) => {
    const query = new URLSearchParams({
      tickers: tickers.join(","),
      news_raw_scale: String(newsRawScale),
      news_score_clip: String(newsScoreClip),
      news_source: newsSource,
      factor_enabled: String(factorEnabled),
      half_life_hours: String(halfLifeHours),
      max_age_hours: String(maxAgeHours),
      direct_weight: String(directWeight),
      industry_weight: String(industryWeight),
      commodity_weight: String(commodityWeight),
      macro_weight: String(macroWeight),
      limit: String(limit),
    });
    if (factorAsOf) query.set("factor_as_of", factorAsOf);
    if (jobId) query.set("job_id", jobId);
    return request(`/api/live/news?${query}`);
  },
};
