import { test, expect } from './fixtures.js';

test('host capacity configuration retains edits during polling and reviews memory', async ({ page, backend }) => {
  backend.jobs = [{ id: 'finished', kind: 'backtest', status: 'completed', started_at: new Date().toISOString(), name: 'Measured run',
    config: { asset_class: 'equity' }, resources: { state: 'running', cpu_slots: 2, memory_gb: 8, peak_rss_gb: 1.25 } }];
  await page.goto('/?asset=equity&workflow=backtest');
  await page.getByText(/Host research capacity/).click();
  const intro = await page.locator('.research-resources form > p').first().boundingBox();
  const firstInput = await page.getByLabel('Research CPU slots', { exact: true }).boundingBox();
  expect(firstInput.y).toBeGreaterThanOrEqual(intro.y + intro.height);
  await expect(page.getByText(/2 \/ 4 CPU slots reserved/)).toBeVisible();
  await page.getByLabel('Research CPU slots', { exact: true }).fill('3');
  await page.getByLabel('Research memory GiB', { exact: true }).fill('12');
  await page.getByLabel('Host capacity operator token', { exact: true }).fill('local-test-token');
  backend.failures['/api/research-resources'] = 503;
  await page.getByRole('button', { name: 'Save host limits', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Limits were not saved' })).toBeVisible();
  delete backend.failures['/api/research-resources'];
  // A successful background refresh must not dismiss an action rejection.
  await expect.poll(() => backend.requests.filter(item => item.path === '/api/research-resources' && item.method === 'GET').length).toBeGreaterThan(1);
  await expect(page.getByRole('alert').filter({ hasText: 'Limits were not saved' })).toBeVisible();
  await expect(page.getByLabel('Research CPU slots', { exact: true })).toHaveValue('3');
  await page.getByRole('button', { name: 'Save host limits', exact: true }).click();
  await expect(page.getByText('Host limits saved.')).toBeVisible();
  expect(backend.requests.filter(item => item.path === '/api/research-resources' && item.method === 'PUT').at(-1).body)
    .toEqual({ cpu_slots: 3, memory_gb: 12 });
  await page.getByText('Completed resource measurements', { exact: true }).click();
  await expect(page.getByText(/sampled peak RSS: 1.25 GiB/)).toBeVisible();
  await page.screenshot({ path: 'test-results/host-capacity-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  for (const summary of await page.locator('.research-resources summary').all()) {
    expect((await summary.boundingBox()).height).toBeGreaterThanOrEqual(44);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByLabel('Research CPU slots', { exact: true }).focus();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Research memory GiB', { exact: true })).toBeFocused();
  await page.screenshot({ path: 'test-results/host-capacity-mobile.png', fullPage: true });
});

test('queued research remains cancellable after reload without claiming active compute', async ({ page, backend }) => {
  const resources = { state: 'queued', queue_position: 2, cpu_slots: 2, memory_gb: 8, peak_rss_gb: null };
  backend.jobs = [{ id: 'queued', kind: 'backtest', name: 'Waiting study', status: 'queued', resources,
    config: { asset_class: 'equity', tickers: ['QQQ'] }, started_at: new Date().toISOString() }];
  await page.goto('/?asset=equity&workflow=backtest');
  await page.locator('.run-list__row.is-job button').first().click();
  await expect(page.getByText(/position 2 · waiting for shared host capacity/)).toBeVisible();
  await expect(page.getByText('LIVE COMPUTE', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Research process', exact: true })).toHaveCount(0);
  await page.reload();
  await page.locator('.run-list__row.is-job button').first().click();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
});
