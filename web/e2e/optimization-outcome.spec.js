import { test, expect } from './fixtures.js';

const diagnostics = {
  completed_trials: 2, qualifying_trials: 0, pruned_trials: 0, failed_trials: 0,
  min_positive_fold_fraction: .6,
  require_active_folds: true,
  rejected_trials: [
    { trial_number: 1, positive_folds: 0, fold_count: 5, required_positive_folds: 3, trades: 24, inactive_folds: 2, reasons: ['0 of 5 positive folds; 3 required.'] },
    { trial_number: 2, positive_folds: 2, fold_count: 5, required_positive_folds: 3, trades: 30, inactive_folds: 2, reasons: ['2 of 5 positive folds; 3 required.', 'Fold 4: no executed trades under normal and stressed costs.', 'Fold 4: whole-share sizing blocked entries. QQQ.IBKR: 14 attempts below one share (largest allowed quantity 0.637; risk cap up to $12.50).'] },
  ],
};

const rejectedRun = {
  run_id: 'search-rejected', name: 'Rejected development search', kind: 'optimize', asset_class: 'equity',
  tickers: ['QQQ'], selection_status: 'NO_QUALIFYING_CANDIDATE', can_resume: true,
  resume_run_id: `study-rejected-${'x'.repeat(80)}`, best_params: {}, in_sample_value: null,
  oos_metrics: null, oos_score: null, oos_equity_curve: [],
  search_diagnostics: diagnostics,
  validation: { outer_holdout_status: 'UNTOUCHED', final_test: null },
};

for (const width of [1440, 390]) {
  test(`rejected search has no winner or holdout and stays reviewable at ${width}px`, async ({ page, backend }) => {
    backend.runs = [rejectedRun];
    await page.route('**/api/runs/search-rejected/research', route => route.fulfill({ json: {
      status: 'insufficient_data', optimization: { selection_status: rejectedRun.selection_status,
        best_params: {}, history: [{ number: 0, state: 'COMPLETE', value: -1000000, incumbent: null }],
        state_counts: { COMPLETE: 1 }, parameter_importance: [], folds: [],
        validation: rejectedRun.validation },
    } }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?asset=equity&workflow=optimize');
    const outcome = page.getByRole('status', { name: 'Optimization outcome' });
    await expect(outcome.getByRole('heading', { name: 'No qualifying candidate' })).toBeVisible();
    await expect(outcome).toContainText('No saved parameters were replaced. The outer holdout was not evaluated.');
    await expect(outcome).toContainText('study-rejected');
    await expect(outcome).toContainText('2 completed trials · 0 qualifying');
    await expect(outcome).toContainText('2 of 5 positive folds; 3 required.');
    await expect(outcome).toContainText('Every validation fold must execute trades under both normal and stressed costs.');
    await expect(outcome).toContainText('QQQ.IBKR: 14 attempts below one share');
    await outcome.getByText('Rejection details · 2 trials', { exact: true }).click();
    const table = outcome.getByRole('table');
    await expect(table.getByRole('row')).toHaveCount(3);
    await expect(table.getByRole('row').nth(1)).toContainText('24');
    await expect(table.getByRole('row').nth(2)).toContainText('risk cap up to $12.50');
    const scrollRegion = outcome.getByRole('region', { name: 'Rejected trial details' });
    await scrollRegion.focus();
    await expect(scrollRegion).toBeFocused();
    await expect(page.getByText('DEVELOPMENT SEARCH · HOLDOUT UNTOUCHED')).toBeVisible();
    await page.getByRole('button', { name: 'Optimization', exact: true }).click();
    await expect(page.getByText('No parameters selected: no candidate qualified.')).toBeVisible();
    await expect(page.getByRole('group', { name: 'OBJECTIVE / TRIAL chart', exact: true }).getByText('No completed trial met the qualification rules')).toBeVisible();
    await expect(page.getByText('Trial 0', { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/rejected-optimization-${width}.png`, fullPage: true });
  });
}

test('development-only result is excluded from execution selection', async ({ page, backend }) => {
  backend.runs = [{ ...rejectedRun, selection_status: 'DEVELOPMENT_SELECTED', best_params: { n_lags: 3 }, in_sample_value: .25 }];
  await page.goto('/?asset=equity&workflow=backtest');
  await page.getByRole('button', { name: /New backtest/ }).click();
  await expect(page.getByRole('combobox', { name: 'Optuna result', exact: true }).getByRole('option')).toHaveCount(1);
});

test('completed job progress does not mark the outer holdout complete when no trial qualifies', async ({ page, backend }) => {
  backend.jobs = [{ id: 'rejected-job', kind: 'optimize', status: 'running', config: { asset_class: 'equity' } }];
  await page.route('**/api/jobs/rejected-job/progress', route => route.fulfill({ json: {
    phase: 'complete', phase_label: 'No qualifying candidate', percent: 100,
    outcome: 'NO_QUALIFYING_CANDIDATE', best_score: null, resume_run_id: 'study-rejected',
    outer_holdout_status: 'UNTOUCHED', history: [],
    search_diagnostics: diagnostics, candidate_score: -1000000,
  } }));
  await page.goto('/?asset=equity&workflow=optimize');
  await page.locator('.run-list__row.is-job button').first().click();
  await expect(page.getByRole('status', { name: 'Optimization outcome' })).toContainText('not evaluated');
  await expect(page.getByText('Outer holdout', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('status', { name: 'Optimization outcome' })).toContainText('2 of 5 positive folds; 3 required.');
  await expect(page.getByRole('group', { name: 'CANDIDATE SCORE chart', exact: true }).getByText('No completed trial met the qualification rules')).toBeVisible();
  await expect(page.locator('.active-research__readings')).toContainText('Not qualifying');
  await expect(page.locator('.active-research__readings')).not.toContainText('-1000000');
});

test('rejected result is excluded from the backtest winner selector', async ({ page, backend }) => {
  backend.runs = [rejectedRun];
  await page.goto('/?asset=equity&workflow=backtest');
  await page.getByRole('button', { name: /New backtest/ }).click();
  await expect(page.getByRole('combobox', { name: 'Optuna result', exact: true }).getByRole('option')).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Optimize & Backtest', exact: true })).toBeVisible();
});

for (const width of [1440, 390]) {
  test(`zero-trade holdout is rejected and excluded from selection at ${width}px`, async ({ page, backend }) => {
    const run = { ...rejectedRun, run_id: 'holdout-rejected', name: 'Inactive holdout',
      selection_status: 'REJECTED_OUTER_HOLDOUT', can_resume: false,
      best_params: { n_lags: 3 }, in_sample_value: .25,
      oos_metrics: { total_trades: 0, net_profit_usd: 0, primary_score: 0 },
      holdout_assessment: { status: 'REJECTED_OUTER_HOLDOUT', reasons: ['Normal-cost holdout has no executed trades.'] },
      validation: { outer_holdout_status: 'CONSUMED', final_test: { trades: 0, normal_ratio: 0, stressed_ratio: 0 } } };
    backend.runs = [run];
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?asset=equity&workflow=optimize');
    const outcome = page.getByRole('status', { name: 'Optimization outcome' });
    await expect(outcome.getByRole('heading', { name: 'Outer holdout rejected' })).toBeVisible();
    await expect(outcome).toContainText('no executed trades');
    await expect(outcome).toContainText('cannot resume tuning');
    await expect(page.getByText('OUTER HOLDOUT REJECTED', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/rejected-holdout-${width}.png`, fullPage: true });
    await page.goto('/?asset=equity&workflow=backtest');
    await page.getByRole('button', { name: /New backtest/ }).click();
    await expect(page.getByRole('combobox', { name: 'Optuna result', exact: true }).getByRole('option')).toHaveCount(1);
  });
}
