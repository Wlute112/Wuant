export const CHART_DEFAULTS = { signals: true, levels: true, forecasts: false, regime: true, context: false, grid: true, crosshair: true, logarithmic: false, type: 'candles', timezone: 'local' };
export const CHART_PRESETS = {
  Monitoring: CHART_DEFAULTS,
  'Price only': { ...CHART_DEFAULTS, signals: false, levels: false, regime: false },
  'Model detail': { ...CHART_DEFAULTS, forecasts: true, context: true },
};
export function normalizeChartPoints(points) {
  const byTime = new Map();
  for (const point of points || []) {
    const time = Date.parse(point.ts);
    if (Number.isFinite(time) && Number.isFinite(point.close)) byTime.set(time, point);
  }
  return [...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([, point]) => point);
}
export function chartWindow(length, count, endTime, points) {
  let end = endTime == null ? length : points.findIndex(p => Date.parse(p.ts) > endTime);
  if (end < 0) end = length;
  end = Math.max(1, Math.min(length, end));
  return { start: Math.max(0, end - count), end };
}
export function readChartSettings(key) {
  try { const stored = JSON.parse(localStorage.getItem(key)); return sanitizeSettings(stored); } catch { return { ...CHART_DEFAULTS }; }
}
export function sanitizeSettings(stored) {
  const result = { ...CHART_DEFAULTS };
  for (const key of Object.keys(result)) if (typeof stored?.[key] === typeof result[key]) result[key] = stored[key];
  if (!['candles', 'line'].includes(result.type)) result.type = 'candles';
  if (!['local', 'UTC', 'America/New_York'].includes(result.timezone)) result.timezone = 'local';
  return result;
}
