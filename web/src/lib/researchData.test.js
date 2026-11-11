import test from 'node:test';
import assert from 'node:assert/strict';
import { dataGuidance, dataRepairStep, missingMetadata } from './researchData.js';

const row = { ticker: 'BTC', bars: 889, zero_volume_bars: 0, source: 'IBKR_LAST',
  session: '24/7', price_basis: 'unadjusted', volume_basis: 'traded_units' };

test('actual MIDPOINT dataset identifies price_basis as the only undeclared field', () => {
  const rows = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'].map(ticker => ({ ...row, ticker,
    zero_volume_bars: 889, source: 'IBKR_MID', price_basis: 'unknown', volume_basis: 'unavailable_midpoint' }));
  const guidance = dataGuidance(rows);
  assert.deepEqual(guidance.metadata, [{ fields: ['price_basis'], tickers: rows.map(r => r.ticker) }]);
  assert.equal(guidance.midpoint.length, 5);
  assert.equal(guidance.zeroVolume.length, 0);
});

test('metadata checks handle blank, whitespace and mixed comma-separated unknown declarations', () => {
  assert.deepEqual(missingMetadata({ ...row, source: ' ', session: 'RTH, Unknown', price_basis: '' }),
    ['source', 'session', 'price_basis']);
  assert.deepEqual(missingMetadata(row), []);
});

test('distinct metadata gaps stay associated with their own tickers', () => {
  const guidance = dataGuidance([{ ...row, price_basis: 'unknown' },
    { ...row, ticker: 'ETH', source: 'unknown', session: 'unknown' }]);
  assert.deepEqual(guidance.metadata, [{ fields: ['price_basis'], tickers: ['BTC'] },
    { fields: ['source', 'session'], tickers: ['ETH'] }]);
});

test('clean data, absent requested tickers and empty reports do not invent limitations', () => {
  for (const rows of [[], [undefined], [row], [row, undefined], [{ ...row, bars: 0 }]]) {
    assert.deepEqual(dataGuidance(rows), { metadata: [], midpoint: [], zeroVolume: [] });
  }
});

test('partial observed zero volume is separated from MIDPOINT data', () => {
  const guidance = dataGuidance([{ ...row, zero_volume_bars: 5 }]);
  assert.equal(guidance.zeroVolume.length, 1);
  assert.equal(guidance.midpoint.length, 0);
});

test('each backend error has an actionable repair, including unknown errors among known ones', () => {
  const errors = ['Cannot read data CSV', 'Missing CSV columns', 'Select at least one ticker',
    'No bars', 'Invalid or missing timestamps', 'Duplicate ticker/timestamp rows',
    'Missing, non-finite or non-positive OHLC prices', 'Invalid OHLC envelope',
    'Missing or invalid volume', 'No positive-volume bars', 'MIDPOINT volume is unavailable',
    'Dataset provenance integrity error', 'Unrecognized supplier error'];
  for (const error of errors) assert.match(dataRepairStep(`BTC: ${error}.`), /Recheck data/);
  assert.match(dataRepairStep('BTC: No bars.'), /named ticker|remove that ticker/);
  assert.match(dataRepairStep('Invalid OHLC envelope'), /high must be at least/);
  assert.match(dataRepairStep('Select at least one ticker'), /Enter at least one ticker/);
  assert.match(dataRepairStep('Unrecognized supplier error'), /data supplier/);
});
