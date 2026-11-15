import { test, expect } from './fixtures.js';

for (const width of [1440, 390]) {
  test(`delete a saved optimization and keep it deleted after reload at ${width}px`, async ({ page, backend }) => {
    const run = { run_id: 'optimize-delete-fixture', name: 'Deletion fixture', kind: 'optimize',
      asset_class: 'equity', tickers: ['QQQ'], selection_status: 'NO_QUALIFYING_CANDIDATE',
      validation: { outer_holdout_status: 'UNTOUCHED' } };
    backend.runs = [run];
    await page.route('**/api/runs/optimize-delete-fixture', async route => {
      if (route.request().method() !== 'DELETE') return route.fallback();
      backend.requests.push({ path: '/api/runs/optimize-delete-fixture', method: 'DELETE' });
      backend.runs = [];
      await route.fulfill({ json: { run_id: run.run_id, deleted: true, job_deleted: true } });
    });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?asset=equity&workflow=optimize');
    const row = page.locator('.run-list__row').filter({ hasText: run.name });
    await expect(row).toBeVisible();
    await row.getByLabel('More actions for Deletion fixture').click();
    page.once('dialog', dialog => dialog.accept());
    await row.getByRole('button', { name: 'Delete run…', exact: true }).click();
    await expect(row).toHaveCount(0);
    expect(backend.requests.filter(r => r.method === 'DELETE')).toHaveLength(1);
    await page.reload();
    await expect(page.locator('.run-list__row')).toHaveCount(0);
    await expect(page.getByText('No runs yet — trigger a backtest or Optuna sweep above.')).toBeVisible();
  });
}
