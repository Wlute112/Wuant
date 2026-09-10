export function sectorEvidenceStatus(evidence, tickers = [], now = Date.now()) {
  const rows = evidence?.classifications;
  if (!Array.isArray(rows) || !rows.length) return "MISSING · entries blocked";
  if (rows.some((row) => !row || typeof row !== "object" || typeof row.symbol !== "string")) {
    return "INVALID · replace evidence before starting";
  }
  if (rows.some((row) => !Number.isFinite(Date.parse(row.valid_until)) || Date.parse(row.valid_until) <= now)) {
    return "EXPIRED · replace evidence before starting";
  }
  const missing = tickers.filter((ticker) => !rows.some((row) => row.symbol === ticker));
  if (missing.length) return `MISSING · ${missing.join(", ")}`;
  return "LOADED · broker identity check at startup";
}

export function executionParamsWithRisk(params, overrides) {
  const result = { ...(params || {}), ...overrides };
  // The runtime gives nested optimizer parameters precedence over top-level
  // values. Apply the operator's explicit risk settings at both levels.
  for (const key of ["params", "best_params"]) {
    if (result[key] && typeof result[key] === "object" && !Array.isArray(result[key])) {
      result[key] = { ...result[key], ...overrides };
    }
  }
  return result;
}

export function telemetryIsFresh(asOf, now = Date.now()) {
  const age = now - Date.parse(asOf);
  return Number.isFinite(age) && age >= 0 && age <= 20000;
}

export function executionEvidenceCurrent({ isDemo, statusUnknown, feedError, executionJob, risk }) {
  return !isDemo && !statusUnknown && !feedError && executionJob?.status === "running"
    && risk?.broker_connectivity?.healthy === true && risk.broker_connectivity.status === "RECONCILED";
}
