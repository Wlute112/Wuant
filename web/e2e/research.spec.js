import { test, expect, configure } from './fixtures.js';

for (const workflow of ['backtest', 'optimize']) {
  test(`${workflow}: configure, launch once, progress, reload, cancel`, async ({ page, backend }) => {
    await configure(page, workflow);
    await page.getByLabel('Tickers', { exact: true }).fill('QQQ');
    await page.getByLabel('Cash', { exact: true }).fill('6200');
    const launch = page.getByRole('button', { name: workflow === 'backtest' ? 'Run Backtest' : 'Start Sweep', exact: true });
    await expect(launch).toBeEnabled();
    await launch.click();
    await expect(page.getByText('Fixture computation', { exact: false }).first()).toBeVisible();
    const requests = backend.requests.filter(r => r.method === 'POST' && r.path === `/api/jobs/${workflow}`);
    expect(requests).toHaveLength(1);
    expect(requests[0].body).toMatchObject({ cash: 6200, tickers: ['QQQ'], asset_class: 'equity' });
    await page.reload();
    await page.locator('.run-list__row.is-job button').first().click();
    await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Stopping…', exact: true })).toBeVisible();
    expect(backend.jobs[0].status).toBe('cancelling');
  });
}

test('preflight blocks missing data, imports CSV and recovers from API failure', async ({ page, backend }) => {
  backend.preflight = { execution_eligible: false, errors: ['Missing ticker QQQ'], tickers: [] };
  await page.goto('/?asset=equity&workflow=backtest');
  await page.getByRole('button', { name: /New backtest/ }).click();
  await expect(page.getByText('Missing ticker QQQ')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run Backtest', exact: true })).toBeDisabled();
  backend.failures['/api/jobs/data/preflight'] = 503;
  await page.getByRole('button', { name: 'Recheck data' }).click();
  await expect(page.getByText('Data status unavailable')).toBeVisible();
  delete backend.failures['/api/jobs/data/preflight'];
  backend.preflight = { execution_eligible: true, tickers: [] };
  await page.locator('input[type=file][accept*="csv"]').setInputFiles({ name: 'browser.csv', mimeType: 'text/csv', buffer: Buffer.from('timestamp,ticker,open,high,low,close,volume\n') });
  await expect(page.getByText('Data preflight passed', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run Backtest', exact: true })).toBeEnabled();
});

test('launch errors retain configuration and API outage is explicitly unknown', async ({ page, backend }) => {
  await configure(page);
  await page.getByLabel('Cash', { exact: true }).fill('7100');
  backend.failures['/api/jobs/backtest'] = 503;
  await page.getByRole('button', { name: 'Run Backtest', exact: true }).click();
  await expect(page.getByText('Injected API failure', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Cash', { exact: true })).toHaveValue('7100');
  await page.keyboard.press('Escape');
  backend.failures['/api/jobs'] = 503;
  await expect(page.getByRole('alert').filter({ hasText: 'Job status is unknown' })).toBeVisible();
  delete backend.failures['/api/jobs'];
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(page.getByText(/Job status is unknown/)).toHaveCount(0);
});

test('calibration upload rejects invalid JSON and exposes accepted evidence', async ({ page, backend }) => {
  await configure(page);
  await page.getByRole('button', { name: 'Show feature / risk / data settings' }).click();
  await page.getByText('Equity execution & costs', { exact: true }).click();
  const upload = page.getByLabel('Dated calibration JSON', { exact: true });
  await upload.setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{}') });
  await expect(page.getByText('The evidence file must contain a JSON array.')).toBeVisible();
  const evidence = [{ ticker: 'QQQ', source: 'Browser calibration fixture', observed_through: '2025-01-01T00:00:00Z', effective_at: '2025-01-02T00:00:00Z', spread_bps: 2 }];
  await upload.setInputFiles({ name: 'evidence.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(evidence)) });
  await page.getByText('Review attached records', { exact: true }).click();
  await expect(page.locator('pre').filter({ hasText: 'Browser calibration fixture' })).toBeVisible();
  await page.getByRole('button', { name: 'Run Backtest', exact: true }).click();
  expect(JSON.stringify(backend.requests.find(r => r.path === '/api/jobs/backtest').body)).toContain('Browser calibration fixture');
});

for (const workflow of ['backtest', 'optimize']) {
  test(`${workflow}: completed result appears and supports all review sections`, async ({ page, backend }) => {
    await configure(page, workflow);
    await page.getByRole('button', { name: workflow === 'backtest' ? 'Run Backtest' : 'Start Sweep', exact: true }).click();
    await expect(page.getByText(/Fixture computation/)).toBeVisible();
    backend.jobs[0].status = 'completed';
    backend.jobs[0].finished_at = new Date().toISOString();
    backend.runs = [{ run_id: `${workflow}-result`, name: 'Completed browser evidence', kind: workflow, asset_class: 'equity', tickers: ['QQQ'], finished_at: new Date().toISOString(), metrics: { net_profit_usd: 125, total_trades: 3 }, equity_curve: [], ml_performance: {} }];
    await expect(page.getByRole('heading', { name: 'Completed browser evidence' })).toBeVisible();
    for (const section of ['Performance', 'Risk & trades', 'Model', 'Evidence']) {
      await page.getByRole('button', { name: section, exact: true }).click();
      await expect(page.getByRole('button', { name: section, exact: true })).toHaveAttribute('aria-current', 'page');
    }
    await expect(page.getByText(/No benchmark fixture/).first()).toBeVisible();
  });
}

test('observed-data repair has settings, result evidence and cancellation', async ({ page, backend }) => {
  await configure(page);
  await page.getByText('Repair with observed IBKR trade bars', { exact: true }).click();
  await page.getByLabel('IBKR port', { exact: true }).fill('4002');
  await page.getByRole('button', { name: 'Fetch observed replacement' }).click();
  await expect(page.getByRole('heading', { name: 'Observed data repair' })).toBeVisible();
  expect(backend.requests.find(r => r.path === '/api/jobs/data/repair').body.ibkr.ibkr_port).toBe(4002);
  await expect(page.getByRole('region', { name: 'Per-ticker data coverage' })).toBeVisible();
  await page.locator('article:visible').getByText('Broker output and backup location', { exact: true }).click();
  await expect(page.locator('article:visible').getByText('Browser fixture process output', { exact: true })).toBeVisible();
  await page.locator('article:visible').getByRole('button', { name: 'Cancel data fetch', exact: true }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
});

test('rapid repeat launch submits a single request while pending', async ({ page, backend }) => {
  await configure(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/jobs/backtest', async route => { await gate; await route.fallback(); });
  const launch = page.getByRole('button', { name: 'Run Backtest', exact: true });
  await launch.evaluate(button => { button.click(); button.click(); button.click(); });
  await expect(page.getByRole('button', { name: 'Starting…', exact: true })).toBeDisabled();
  release();
  await expect(page.getByText(/Fixture computation/)).toBeVisible();
  expect(backend.requests.filter(r => r.path === '/api/jobs/backtest')).toHaveLength(1);
});

test('narrow viewport retains configuration and keyboard dismissal restores focus', async ({ page, backend }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await configure(page);
  await page.getByLabel('Cash', { exact: true }).fill('8100');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: /New backtest/ })).toBeFocused();
  await page.getByRole('button', { name: /New backtest/ }).press('Enter');
  await expect(page.getByLabel('Cash', { exact: true })).toHaveValue('8100');
  await page.screenshot({ path: 'test-results/narrow-configuration.png', fullPage: true });
});

test('dataset snapshot, removed universe and declarations are reviewable and downloadable', async ({ page, backend }) => {
  backend.preflight.provenance = {
    status: 'archived', sha256: 'b'.repeat(64), manifest_sha256: 'c'.repeat(64), observed_at: '2026-09-21T00:00:00Z',
    changes: { added_symbols: ['QQQ'], removed_symbols: ['DELISTED'], added_bars: 5, removed_bars: 2, revised_bars: 1 },
    universe: [{ ticker: 'QQQ', bars: 300, observed_start: '2025-01-02', observed_end: '2026-01-02',
      declarations: [{ source: 'Dated supplier fixture', retrieved_at: '2026-09-20', con_id: '123', membership_start: '2024-01-01' }] }],
    history: [{ sha256: 'a'.repeat(64), observed_at: '2026-09-19', operation: 'csv_import', changes: null }],
    limitations: ['Survivorship and delisted-instrument coverage are unknown.'],
  };
  await configure(page);
  await page.getByText('Dataset provenance & universe · Archived snapshot', { exact: true }).click();
  await expect(page.getByText(/Removed symbols: DELISTED/)).toBeVisible();
  await page.getByText('Dated supplier fixture · retrieved 2026-09-20', { exact: true }).click();
  await expect(page.getByText('Membership start', { exact: true })).toBeVisible();
  await expect(page.getByText('2024-01-01', { exact: true })).toBeVisible();
  await expect(page.getByText('Unknown / undeclared').first()).toBeVisible();
  await page.getByText('Earlier dataset observations (1)', { exact: true }).click();
  await expect(page.getByText('2026-09-19 · csv_import', { exact: true })).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download provenance evidence' }).click();
  expect((await download).suggestedFilename()).toBe(`dataset-${'b'.repeat(64)}.json`);
  await page.screenshot({ path: "test-results/dataset-provenance-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('region', { name: 'Dataset universe and lineage' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/dataset-provenance-mobile.png', fullPage: true });
});

test('saved research shows retained provenance and legacy evidence remains unknown', async ({ page, backend }) => {
  backend.runs = [{ run_id: 'old-result', name: 'Legacy result', kind: 'backtest', asset_class: 'equity', tickers: ['QQQ'], metrics: {}, equity_curve: [], ml_performance: {} }];
  await page.goto('/?asset=equity&workflow=backtest');
  await expect(page.getByRole('heading', { name: 'Legacy result' })).toBeVisible();
  await expect(page.getByText(/Dataset provenance unavailable for this record/)).toBeVisible();
});
