import { test, expect } from './fixtures.js';
for (const width of [1440, 390]) {
  test(`fold evidence is inspectable at ${width}px`, async ({ page, backend }) => {
    backend.jobs = [{ id: 'evidence-job', kind: 'optimize', status: 'running', config: { asset_class: 'equity' } }];
    const summary = { trial: 1, state: 'complete', score: .8, params: { horizon: 3 }, folds_complete: 1, reason: 'Development evaluation complete; holdout approval is separate.' };
    const result = { ratio: 1.2, trades: 1, turnover: .2, net_profit_usd: 20, detail: {
      equity: [{ ts: '2025-01-06T21:00:00Z', equity: 1000 }, { ts: '2025-01-07T21:00:00Z', equity: 1020 }], equity_total: 2,
      fills: { total: 1, rows: [{ side: 'BUY', ts_last: '2025-01-06T21:00:00Z', avg_px: 100, instrument_id: 'QQQ' }] } } };
    const detail = { ...summary, effective_params: { horizon: 3 }, score_components: { median_ratio: 1.2 }, folds: [{ fold: 1, training_start: '2024-01-01', training_end: '2024-12-30', embargo_bars: 3, validation_start: '2025-01-06T21:00:00Z', validation_end: '2025-01-07T21:00:00Z', normal: result, stressed: { ...result, ratio: .9 }, benchmark: { status: 'unavailable', message: 'No benchmark coverage for this fixture.' } }] };
    await page.route('**/api/jobs/evidence-job/progress', route => route.fulfill({ json: { phase: 'search', trial_current: 1, history: [{ trial: 1, score: .8, best: .8, state: 'complete' }] } }));
    await page.route('**/api/jobs/evidence-job/evidence*', route => route.fulfill({ json: new URL(route.request().url()).searchParams.has('trial') ? detail : { trials: [summary] } }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?asset=equity&workflow=optimize');
    await page.getByRole('button', { name: /live Optuna sweep/ }).click();
    const panel = page.getByRole('region', { name: 'Optimization evidence' });
    await expect(panel.getByRole('heading', { name: 'Inside the search' })).toBeVisible();
    await expect(panel.getByText('No benchmark coverage for this fixture.', { exact: false })).toBeVisible();
    await expect(panel.locator('.channel-strip__overlay-label')).toHaveCount(0);
    await panel.getByLabel('Cost assumption').selectOption('stressed');
    await expect(panel.getByRole('list', { name: 'Validation boundaries' })).toBeVisible();
    await panel.getByText('Parameters and changes from the development leader').click();
    await expect(panel.getByText('horizon', { exact: true })).toBeVisible();
    await panel.getByText('Filled orders · 1', { exact: true }).click();
    await expect(panel.getByRole('cell', { name: 'QQQ', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/optimization-evidence-${width}.png`, fullPage: true });
  });

  test(`rejected trial explains whole-share blockers at ${width}px`, async ({ page, backend }) => {
    backend.jobs = [{ id: 'sizing-job', kind: 'optimize', status: 'running', config: { asset_class: 'equity' } }];
    const summary = { trial: 40, state: 'complete', score: -1000000, params: { atr_stop_mult: 3.855 }, folds_complete: 5, reason: 'Development rejected. 2 of 5 positive folds; 3 required.' };
    const detail = { ...summary, folds: [], development_assessment: { passed: false, reasons: [
      '2 of 5 positive folds; 3 required.',
      'Fold 4: no executed trades under normal and stressed costs.',
      'Fold 4: whole-share sizing blocked entries. QQQ.IBKR: 14 attempts below one share (largest allowed quantity 0.637; risk cap up to $12.50).',
    ] } };
    await page.route('**/api/jobs/sizing-job/progress', route => route.fulfill({ json: { phase: 'search', evidence_available: true } }));
    await page.route('**/api/jobs/sizing-job/evidence*', route => route.fulfill({ json: new URL(route.request().url()).searchParams.has('trial') ? detail : { trials: [summary] } }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?asset=equity&workflow=optimize');
    await page.getByRole('button', { name: /live Optuna sweep/ }).click();
    const panel = page.getByRole('region', { name: 'Optimization evidence' });
    await expect(panel.getByRole('heading', { name: 'Why this trial was rejected' })).toBeVisible();
    await expect(panel).toContainText('QQQ.IBKR: 14 attempts below one share');
    await expect(panel).toContainText('Best qualifying captured trial: none yet');
    await expect(panel).not.toContainText('-1000000');
    await panel.getByText('Score components', { exact: true }).click();
    await expect(panel).toContainText('execute trades in every fold under both cost assumptions');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/sizing-rejection-${width}.png`, fullPage: true });
  });
}
