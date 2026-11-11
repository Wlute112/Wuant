import { test, expect, configure } from './fixtures.js';

for (const width of [1440, 390]) {
  test(`optimization explains warning-only MIDPOINT data and the recovery at ${width}px`, async ({ page, backend }) => {
    await page.setViewportSize({ width, height: 1000 });
    const tickers = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];
    backend.preflight = { ...backend.preflight, errors: [], execution_eligible: true,
      tickers: tickers.map(ticker => ({ ...backend.preflight.tickers[0], ticker, source: 'IBKR_MID',
        bars: 889, zero_volume_bars: 889, session: '24/7', price_basis: 'unknown', volume_basis: 'unavailable_midpoint' })) };
    await page.goto('/?asset=crypto&workflow=optimize');
    await page.getByRole('button', { name: /New sweep/ }).click();
    const section = page.getByRole('region', { name: 'Research data preflight', exact: true });
    const guidance = section.getByLabel('Data limitations and next steps', { exact: true });
    await expect(section.getByRole('status')).toHaveText('Data usable with limitations');
    await expect(section.locator('.data-checks__passed')).toHaveCount(0);
    await expect(guidance).toContainText('Non-blocking data limitations');
    await expect(guidance).toContainText('BTC, ETH, SOL, XRP, DOGE: undeclared price_basis.');
    await expect(guidance).toContainText('MIDPOINT bars provide prices without observed trade volume');
    await expect(guidance).toContainText('Choose Data CSV');
    await expect(guidance).toContainText('Recheck data');
    await expect(section.getByRole('alert')).toHaveCount(0);
    await expect(section.getByRole('region', { name: 'Per-ticker data coverage' })).toBeHidden();
    await expect(page.getByRole('button', { name: 'Start Sweep', exact: true })).toBeEnabled();
    await guidance.scrollIntoViewIfNeeded();
    expect(await page.getByRole('dialog').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await section.getByText('Data tools & import options', { exact: true }).click();
    await section.getByText('Repair with observed IBKR trade bars', { exact: true }).click();
    await expect(section.getByText('Crypto trade bars use all available hours (24/7).', { exact: true })).toBeVisible();
    await expect(section.getByLabel('Session', { exact: true })).toHaveCount(0);
    await section.getByLabel('IBKR port', { exact: true }).fill('4002');
    await section.getByRole('combobox', { name: 'Bar width', exact: true }).selectOption('24');
    await section.getByRole('button', { name: 'Fetch observed replacement', exact: true }).click();
    expect(backend.requests.find(r => r.path === '/api/jobs/data/repair').body).toMatchObject({
      asset_class: 'crypto', tickers, ibkr: { ibkr_port: 4002, ibkr_bar_hours: 24 },
    });
    backend.jobs[0].status = 'completed';
    await page.getByRole('button', { name: /New sweep/ }).click();
    await page.screenshot({ path: `test-results/data-guidance-${width}.png`, fullPage: true });
    backend.preflight.tickers = backend.preflight.tickers.map(row => ({ ...row, source: 'IBKR_LAST',
      zero_volume_bars: 0, price_basis: 'unadjusted', volume_basis: 'traded_units' }));
    await section.getByRole('button', { name: 'Recheck data', exact: true }).click();
    await expect(guidance).toHaveCount(0);
    await expect(section.locator('.data-checks__item.is-passed')).toHaveCount(4);
  });
}

test('optimization pairs every blocker with a next step and handles an absent requested ticker', async ({ page, backend }) => {
  const errors = ['QQQ: Missing or invalid volume.', 'QQQ: Invalid OHLC envelope.',
    'ETH: No bars.', 'QQQ: Unrecognized supplier error.'];
  backend.preflight.execution_eligible = false;
  backend.preflight.errors = [...errors, errors[0]];
  await page.goto('/?asset=equity&workflow=optimize');
  await page.getByRole('button', { name: /New sweep/ }).click();
  await page.getByLabel('Tickers', { exact: true }).fill('QQQ,ETH');
  const section = page.getByRole('region', { name: 'Research data preflight', exact: true });
  const alert = section.getByRole('alert');
  for (const error of errors) await expect(alert.getByText(error, { exact: true })).toHaveCount(1);
  await expect(alert.getByText('Next step:', { exact: true })).toHaveCount(4);
  await expect(alert).toContainText('high must be at least open, close and low');
  await expect(alert).toContainText('data supplier');
  await expect(page.getByRole('button', { name: 'Start Sweep', exact: true })).toBeDisabled();
});

test('optimization summarizes data checks and keeps evidence and repair tools accessible', async ({ page, backend }) => {
  const tickers = ['NVDA','AAPL','MSFT','GOOGL','AMZN','AVGO','TSLA','META','BRK-B','LLY','MU','WMT','AMD','JPM','XOM','V','JNJ','MA','ABBV','INTC'];
  const row = backend.preflight.tickers[0];
  backend.preflight = { ...backend.preflight, warnings: [], tickers: tickers.map(ticker => ({ ...row, ticker, bars: 1258, source: 'IBKR_TRADES' })) };
  await configure(page, 'optimize');
  await page.getByLabel('Tickers', { exact: true }).fill(tickers.join(','));
  const section = page.getByRole('region', { name: 'Research data preflight', exact: true });
  await expect(section.getByText('20 / 20 tickers covered', { exact: true })).toBeVisible();
  await expect(section.locator('.data-checks__item.is-passed')).toHaveCount(4);
  await expect(section.getByText(/25,160 bars/)).toBeVisible();
  await expect(section.getByRole('region', { name: 'Per-ticker data coverage', exact: true })).toBeHidden();
  await expect(section.getByText('Repair with observed IBKR trade bars', { exact: true })).toBeHidden();
  await section.getByText('View data details', { exact: true }).click();
  await expect(section.getByRole('region', { name: 'Per-ticker data coverage', exact: true })).toBeVisible();
  await section.getByText('View data details', { exact: true }).click();
  const tools = section.getByText('Data tools & import options', { exact: true });
  await tools.focus();
  await page.keyboard.press('Enter');
  await section.getByText('Repair with observed IBKR trade bars', { exact: true }).click();
  await expect(section.getByRole('button', { name: 'Fetch observed replacement', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start Sweep', exact: true })).toBeEnabled();
});

test('optimization leaves blockers visible and clears confirmations when data status is unknown', async ({ page, backend }) => {
  backend.preflight.execution_eligible = false;
  backend.preflight.errors = ['QQQ: Missing or invalid volume. Import observed volume.'];
  backend.preflight.tickers[0].missing_volume_bars = 10;
  backend.preflight.tickers[0].source = 'unknown';
  await page.goto('/?asset=equity&workflow=optimize');
  await page.getByRole('button', { name: /New sweep/ }).click();
  const section = page.getByRole('region', { name: 'Research data preflight', exact: true });
  await expect(section.getByRole('alert')).toContainText('QQQ: Missing or invalid volume.');
  await expect(section.getByText('Volume needs repair', { exact: true })).toBeVisible();
  await expect(section.getByText('Metadata incomplete', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start Sweep', exact: true })).toBeDisabled();
  backend.failures['/api/jobs/data/preflight'] = 503;
  await section.getByRole('button', { name: 'Recheck data', exact: true }).click();
  await expect(section.getByText('Data status unavailable', { exact: true })).toBeVisible();
  await expect(section.locator('.data-checks__item.is-passed')).toHaveCount(0);
  delete backend.failures['/api/jobs/data/preflight'];
  backend.preflight.execution_eligible = true;
  backend.preflight.errors = [];
  backend.preflight.tickers[0].missing_volume_bars = 0;
  backend.preflight.tickers[0].source = 'IBKR_TRADES';
  await section.getByRole('button', { name: 'Recheck data', exact: true }).click();
  await expect(section.locator('.data-checks__item.is-passed')).toHaveCount(4);
  await expect(page.getByRole('button', { name: 'Start Sweep', exact: true })).toBeEnabled();
});

test('compact optimization preserves synthetic warnings on narrow screens', async ({ page, backend }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  backend.preflight.tickers[0].source = 'synthetic_fixture';
  backend.preflight.tickers[0].bars = 12;
  const filename = 'sp500_top20_ibkr_daily_5y-a35f64036a-long-dataset-name.csv';
  await page.route('**/api/jobs/upload-csv*', route => route.fulfill({ json: {
    path: 'quant/jobs/uploads/long.csv', filename,
  } }));
  await configure(page, 'optimize');
  await page.locator('input[type=file][accept*="csv"]').setInputFiles({
    name: filename,
    mimeType: 'text/csv', buffer: Buffer.from('timestamp,ticker,open,high,low,close,volume\n'),
  });
  const section = page.getByRole('region', { name: 'Research data preflight', exact: true });
  await expect(section.getByRole('note')).toContainText('Synthetic data. For pipeline testing only');
  await expect(section.getByRole('note')).toContainText('150 warmup bars');
  await expect(section.getByRole('region', { name: 'Per-ticker data coverage', exact: true })).toBeHidden();
  expect(await page.getByRole('dialog').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/optimization-checks-mobile.png', fullPage: true });
});

test('partial benchmark coverage cannot display a full-period comparison', async ({ page, backend }) => {
  backend.runs = [{ run_id: 'partial-benchmark', kind: 'backtest', asset_class: 'equity', tickers: ['QQQ'], metrics: {} }];
  await page.route('**/api/runs/partial-benchmark/research', route => route.fulfill({ json: {
    status: 'ready', period: { start: '2024-01-02', end: '2024-02-01' },
    strategy: { total_return_pct: 10 },
    benchmark: { status: 'partial', message: 'Partial S&P 500 coverage; same-period comparisons unavailable.', metrics: { total_return_pct: 999 } },
    series: { strategy_index: [{ ts: '2024-01-02', value: 100 }, { ts: '2024-02-01', value: 110 }],
      benchmark_index: [{ ts: '2024-01-02', value: 100 }, { ts: '2024-01-15', value: 1099 }],
      strategy_drawdown: [{ ts: '2024-01-02', value: 0 }, { ts: '2024-02-01', value: -1 }],
      benchmark_drawdown: [{ ts: '2024-01-02', value: 0 }, { ts: '2024-01-15', value: -99 }] },
  } }));
  await page.goto('/?asset=equity&workflow=backtest');
  await expect(page.getByText('Partial S&P 500 coverage; same-period comparisons unavailable.', { exact: true })).toBeVisible();
  await expect(page.getByText('Strategy rebased to 100 · same-period benchmark unavailable', { exact: true })).toBeVisible();
  const row = page.locator('.metric-ledger__row').filter({ hasText: 'Total return' });
  await expect(row.locator('strong')).toHaveText('10.00%');
  await expect(row.locator('span').nth(1)).toHaveText('—');
  await expect(row.locator('span').nth(2)).toHaveText('—');
  await expect(page.locator('.channel-strip__overlay-label')).toHaveCount(0);
  await page.getByRole('button', { name: 'Risk & trades', exact: true }).click();
  await expect(page.locator('.channel-strip__overlay-label')).toHaveCount(0);
});

for (const workflow of ['backtest', 'optimize']) {
  test(`${workflow}: configure, launch once, progress, reload, cancel`, async ({ page, backend }) => {
    await configure(page, workflow);
    await page.getByLabel('Tickers', { exact: true }).fill('QQQ');
    await page.getByLabel('Cash', { exact: true }).fill('6200');
    const launch = page.getByRole('button', { name: workflow === 'backtest' ? 'Optimize & Backtest' : 'Start Sweep', exact: true });
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
  await expect(page.getByRole('button', { name: 'Optimize & Backtest', exact: true })).toBeDisabled();
  backend.failures['/api/jobs/data/preflight'] = 503;
  await page.getByRole('button', { name: 'Recheck data' }).click();
  await expect(page.getByText('Data status unavailable')).toBeVisible();
  delete backend.failures['/api/jobs/data/preflight'];
  backend.preflight = { execution_eligible: true, tickers: [] };
  await page.locator('input[type=file][accept*="csv"]').setInputFiles({ name: 'browser.csv', mimeType: 'text/csv', buffer: Buffer.from('timestamp,ticker,open,high,low,close,volume\n') });
  await expect(page.getByText('Data checks passed', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Optimize & Backtest', exact: true })).toBeEnabled();
});

test('launch errors retain configuration and API outage is explicitly unknown', async ({ page, backend }) => {
  await configure(page);
  await page.getByLabel('Cash', { exact: true }).fill('7100');
  backend.failures['/api/jobs/backtest'] = 503;
  await page.getByRole('button', { name: 'Optimize & Backtest', exact: true }).click();
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
  await page.getByRole('button', { name: 'Show risk / data settings' }).click();
  await page.getByText('Equity execution & costs', { exact: true }).click();
  const upload = page.getByLabel('Dated calibration JSON', { exact: true });
  await upload.setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{}') });
  await expect(page.getByText('The evidence file must contain a JSON array.')).toBeVisible();
  const evidence = [{ ticker: 'QQQ', source: 'Browser calibration fixture', observed_through: '2025-01-01T00:00:00Z', effective_at: '2025-01-02T00:00:00Z', spread_bps: 2 }];
  await upload.setInputFiles({ name: 'evidence.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(evidence)) });
  await page.getByText('Review attached records', { exact: true }).click();
  await expect(page.locator('pre').filter({ hasText: 'Browser calibration fixture' })).toBeVisible();
  await page.getByRole('button', { name: 'Optimize & Backtest', exact: true }).click();
  expect(JSON.stringify(backend.requests.find(r => r.path === '/api/jobs/backtest').body)).toContain('Browser calibration fixture');
});

for (const workflow of ['backtest', 'optimize']) {
  test(`${workflow}: completed result appears and supports all review sections`, async ({ page, backend }) => {
    await configure(page, workflow);
    await page.getByRole('button', { name: workflow === 'backtest' ? 'Optimize & Backtest' : 'Start Sweep', exact: true }).click();
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

test('synthetic NYSE fixture can be configured, cancelled, reviewed and selected', async ({ page, backend }) => {
  await configure(page);
  await page.getByText('Generate synthetic NYSE session fixture', { exact: true }).click();
  await page.getByLabel('Sessions', { exact: true }).fill('12');
  await page.getByLabel('First eligible date', { exact: true }).fill('2026-11-23');
  await page.getByLabel('Seed (optional)', { exact: true }).fill('42');
  await page.getByRole('button', { name: 'Generate fixture', exact: true }).click();
  const request = backend.requests.find(item => item.path === '/api/jobs/data/synthetic-fixture');
  expect(request.body).toMatchObject({ asset_class: 'equity', days: 12, start: '2026-11-23', seed: 42 });
  await expect(page.getByText('Generating XNYS fixture', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel generation' }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
  backend.jobs[0].status = 'completed';
  await expect(page.getByText(/XNYS calendar v4.13.2 · 1 early closes/)).toBeVisible();
  await page.getByRole('button', { name: 'Use fixture for research' }).click();
  await expect(page.locator('#data-csv-file-status')).toContainText('XNYS synthetic fixture · synthetic_fixture-test-1.csv');
  await expect.poll(() => backend.requests.filter(item => item.path === '/api/jobs/data/preflight').at(-1)?.body?.csv)
    .toBe('quant/jobs/fixtures/synthetic_fixture-test-1.csv');
  await expect(page.getByRole('note').filter({ hasText: 'Synthetic fixture.' })).toBeVisible();
  await expect(page.getByText('12 verified sessions · 1 early closes')).toBeVisible();
  await page.getByLabel('Tickers', { exact: true }).fill('SPY');
  await expect(page.getByRole('button', { name: 'Use fixture for research' })).toHaveCount(0);
  await page.getByLabel('Tickers', { exact: true }).fill('QQQ');
  await expect(page.getByRole('button', { name: 'Use fixture for research' })).toBeVisible();
  await page.screenshot({ path: 'test-results/synthetic-fixture-review.png', fullPage: true });
  await page.reload();
  await page.getByRole('button', { name: /New backtest/ }).click();
  await expect(page.locator('#data-csv-file-status')).toContainText('quant/jobs/fixtures/synthetic_fixture-test-1.csv');
});

test('rapid repeat launch submits a single request while pending', async ({ page, backend }) => {
  await configure(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/jobs/backtest', async route => { await gate; await route.fallback(); });
  const launch = page.getByRole('button', { name: 'Optimize & Backtest', exact: true });
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
