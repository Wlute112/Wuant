/** Pure transforms from a run artifact into the channel-strip point series. */

export function equityPoints(run) {
  const curve = run?.equity_curve ?? run?.oos_equity_curve ?? [];
  return curve.map((p) => ({ x: Date.parse(p.ts), y: p.equity }));
}

export function drawdownPoints(run) {
  const curve = run?.equity_curve ?? run?.oos_equity_curve ?? [];
  let peak = -Infinity;
  return curve.map((p) => {
    peak = Math.max(peak, p.equity);
    const ddPct = peak > 0 ? ((p.equity - peak) / peak) * 100 : 0;
    return { x: Date.parse(p.ts), y: ddPct };
  });
}

export function mlPerformancePoints(run, ticker) {
  const perf = run?.ml_performance?.[ticker];
  if (!perf || !Array.isArray(perf.folds)) return [];
  return perf.folds.map((f) => ({ x: f.fold, y: f.oos_r2 }));
}

export function regimePoints(run, ticker) {
  const series = run?.regime?.[ticker];
  if (!Array.isArray(series)) return [];
  return series.map((p) => ({ x: Date.parse(p.ts), y: p.regime_score }));
}

export function actualPricePoints(run, ticker) {
  const series = run?.ml_performance?.[ticker]?.price_series;
  if (!Array.isArray(series)) return [];
  return series.map((p) => ({ x: Date.parse(p.ts), y: p.actual_price }));
}

export function predictedPricePoints(run, ticker) {
  const series = run?.ml_performance?.[ticker]?.price_series;
  if (!Array.isArray(series)) return [];
  return series.map((p) => ({ x: Date.parse(p.ts), y: p.predicted_price }));
}

export function runMetrics(run) {
  return run?.metrics ?? run?.oos_metrics ?? null;
}

export function mlSummaryMetrics(run, ticker) {
  const perf = run?.ml_performance?.[ticker];
  if (!perf || perf.error) return null;
  return {
    oos_r2: perf.oos_r2,
    directional_accuracy: perf.directional_accuracy,
    information_coefficient: perf.information_coefficient,
  };
}

export function runTickers(run) {
  return run?.tickers ?? [];
}

export function optunaTrialsCount(run) {
  if (!run || run.kind !== "optimize" || !Array.isArray(run.trials)) return null;
  return run.trials.length;
}
