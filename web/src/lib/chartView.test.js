import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeChartPoints, chartWindow, sanitizeSettings, CHART_DEFAULTS, CHART_PRESETS } from './chartView.js';
const point = (day, close = day) => ({ ts: `2026-01-${String(day).padStart(2, '0')}T00:00:00Z`, close });
test('chart adapter sorts, deduplicates by timestamp and retains gaps without inventing fills', () => {
  const normalized = normalizeChartPoints([point(3), point(1), point(3, 9), { ts: 'invalid', close: 1 }, point(2, NaN)]);
  assert.deepEqual(normalized, [point(1), point(3, 9)]);
  assert.equal(normalized[0].fill, undefined);
});
test('inspection remains anchored to timestamp when new bars arrive', () => {
  const initial = [1,2,3,4,5].map(n => point(n));
  const anchor = Date.parse(initial[3].ts);
  assert.deepEqual(chartWindow(5, 2, anchor, initial), { start: 2, end: 4 });
  assert.deepEqual(chartWindow(6, 2, anchor, [...initial, point(6)]), { start: 2, end: 4 });
  assert.deepEqual(chartWindow(6, 2, null, [...initial, point(6)]), { start: 4, end: 6 });
});
test('settings reject unknown types and invalid enum values', () => {
  assert.deepEqual(sanitizeSettings({ forecasts: 'yes', type: 'bad', timezone: 'invalid', unknown: 1 }), CHART_DEFAULTS);
  assert.equal(sanitizeSettings({ forecasts: true }).forecasts, true);
  assert.equal(CHART_PRESETS.Monitoring.forecasts, false);
  assert.equal(CHART_PRESETS['Price only'].signals, false);
});
test('empty chart input and view bounds are safe', () => {
  assert.deepEqual(normalizeChartPoints(null), []);
  assert.deepEqual(chartWindow(2, 100, null, [point(1), point(2)]), { start: 0, end: 2 });
});
