import { test, expect, configure } from './fixtures.js';

for (const width of [1440, 390]) {
  test(`strategy parameters are automatic and stale manual settings are discarded at ${width}px`, async ({ page, backend }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(() => localStorage.setItem('quant-dashboard.action-settings.v1', JSON.stringify({
      features: { n_lags: 30, use_hmm_feature: false }, searchModes: { n_lags: 'manual' },
      loadedParams: { entry_threshold: 0.8 }, tradingParams: { n_lags: 30 },
    })));
    await configure(page, 'optimize');
    await page.getByRole('button', { name: 'Show risk / data settings' }).click();
    await expect(page.getByLabel('AR lag count', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('option', { name: 'Manual', exact: true })).toHaveCount(0);
    const dialog = page.getByRole('dialog');
    expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: `test-results/optuna-settings-${width}.png`, fullPage: true });
    await page.getByRole('button', { name: 'Start Sweep', exact: true }).click();
    const request = backend.requests.find(r => r.path === '/api/jobs/optimize');
    expect(request.body).not.toHaveProperty('features');
    expect(request.body).not.toHaveProperty('params');
    expect(request.body).not.toHaveProperty('params_path');
    expect(request.body).toHaveProperty('risk');
    expect(request.body.allow_shorts).toBe(false);
  });
}

test('automatic backtest opens Optuna progress and supports cancellation', async ({ page, backend }) => {
  await configure(page);
  await page.route('**/api/jobs/backtest', async route => {
    const job = { id: 'optimize-auto', kind: 'optimize', status: 'running', run_id: 'optimize-result',
      config: { asset_class: 'equity' }, started_at: new Date().toISOString() };
    backend.jobs.push(job);
    await route.fulfill({ status: 202, json: job });
  });
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const dialog = page.getByRole('dialog');
    expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: `test-results/automatic-backtest-${width}.png`, fullPage: true });
  }
  await expect(page.getByRole('combobox', { name: 'Optuna result', exact: true })).toHaveValue('');
  await page.getByRole('button', { name: 'Optimize & Backtest', exact: true }).click();
  await expect(page.getByText(/Fixture computation/).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /New sweep/ })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
});

test('backtest replays a saved Optuna ID without sending editable parameters', async ({ page, backend }) => {
  backend.runs = [{ run_id: 'optuna-winner', kind: 'optimize', asset_class: 'equity', allow_short_positions: false, tickers: ['QQQ'] }];
  backend.details['optuna-winner'] = { ...backend.runs[0], best_params: { n_lags: 9 }, bar_interval_minutes: 240 };
  await configure(page);
  await page.getByRole('combobox', { name: 'Optuna result', exact: true }).selectOption('optuna-winner');
  await expect(page.getByText(/Loaded 1 param/)).toBeVisible();
  await page.getByRole('button', { name: 'Run Backtest', exact: true }).click();
  const body = backend.requests.find(r => r.path === '/api/jobs/backtest').body;
  expect(body.source_run_id).toBe('optuna-winner');
  expect(body).not.toHaveProperty('params');
  expect(body).not.toHaveProperty('features');
});

test('unavailable saved result gives a recovery message and locks paper launch', async ({ page, backend }) => {
  backend.runs = [{ run_id: 'optuna-missing', kind: 'optimize', asset_class: 'equity', allow_short_positions: false }];
  backend.failures['/api/runs/optuna-missing'] = 404;
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByRole('button', { name: 'Session controls' }).click();
  await page.getByRole('combobox', { name: 'Optuna result', exact: true }).selectOption('optuna-missing');
  await expect(page.getByText(/Could not load the Optuna result/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start Paper Trading', exact: true })).toBeDisabled();
});

for (const width of [1440, 390]) {
  test(`short policy follows the selected result at ${width}px`, async ({ page, backend }) => {
    backend.runs = [
      { run_id: 'known-short', kind: 'optimize', asset_class: 'equity', allow_short_positions: true },
      { run_id: 'unknown-policy', kind: 'optimize', asset_class: 'equity' },
    ];
    backend.details['known-short'] = { ...backend.runs[0], best_params: { n_lags: 9 } };
    await page.setViewportSize({ width, height: 1000 });
    await configure(page);
    const selector = page.getByRole('combobox', { name: 'Optuna result', exact: true });
    await expect(selector.locator('option[value="unknown-policy"]')).toHaveCount(0);
    await selector.selectOption('known-short');
    const control = page.getByRole('checkbox', { name: 'Simulate short entries', exact: true });
    await expect(control).toBeChecked();
    await control.focus();
    await expect(control).toBeFocused();
    await page.screenshot({ path: `test-results/short-policy-${width}.png`, fullPage: true });
    await page.getByRole('button', { name: 'Run Backtest', exact: true }).click();
    expect(backend.requests.find(r => r.path === '/api/jobs/backtest').body.allow_shorts).toBe(true);
  });
}
