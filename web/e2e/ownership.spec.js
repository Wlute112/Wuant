import { test, expect } from './fixtures.js';

const owned = {
  status: 'OWNED', active: true,
  owner: { owner_id: 'strategy:paper-test', mode: 'paper', asset_class: 'equity', client_id: 1, pid: 123,
    acquired_at: '2026-10-20T15:00:00Z' },
};

test('ownership review stops the owning job and does not claim release before shutdown', async ({ page, backend }) => {
  backend.jobs = [{ id: 'paper-test', kind: 'paper', name: 'Owned equity book', status: 'running',
    config: { asset_class: 'equity', tickers: ['QQQ'] } }];
  backend.ownership = structuredClone(owned);
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByText('Broker execution ownership · OWNED', { exact: true }).click();
  await expect(page.getByText('Owned equity book', { exact: true })).toBeVisible();
  await expect(page.getByText(/Paused runners retain ownership/)).toBeVisible();
  await page.getByRole('button', { name: 'Stop owning session', exact: true }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
  await expect(page.getByText('Broker execution ownership · OWNED', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stopping owner…', exact: true })).toBeDisabled();
  backend.jobs[0].status = 'cancelled';
  backend.ownership = { status: 'AVAILABLE', active: false };
  await expect(page.getByText('Broker execution ownership · AVAILABLE', { exact: true })).toBeVisible();
  await expect(page.getByText(/This does not prove broker orders or positions are flat/)).toBeVisible();
  expect(backend.requests.some(item => item.path.includes('unlock') || item.path.includes('takeover'))).toBe(false);
  await page.screenshot({ path: 'test-results/execution-ownership-desktop.png', fullPage: true });
});

test('CLI ownership and network failures stay explicit on narrow screens', async ({ page, backend }) => {
  backend.ownership = { ...structuredClone(owned), owner: { ...owned.owner, owner_id: 'cli:outside-dashboard' } };
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByText('Broker execution ownership · OWNED', { exact: true }).click();
  await expect(page.getByText(/Stop it with Ctrl-C in its terminal/)).toBeVisible();
  expect((await page.locator('.execution-ownership > summary').boundingBox()).height).toBeGreaterThanOrEqual(44);
  backend.failures['/api/execution-ownership'] = 503;
  await expect(page.getByText('Broker execution ownership · UNKNOWN', { exact: true })).toBeVisible();
  await expect(page.getByText(/Last reported owner/)).toBeVisible();
  await expect(page.getByText(/Execution ownership is unknown:/)).toBeVisible();
  delete backend.failures['/api/execution-ownership'];
  await page.getByRole('button', { name: 'Retry ownership status', exact: true }).click();
  await expect(page.getByText('Broker execution ownership · OWNED', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/execution-ownership-mobile.png', fullPage: true });
});

test('hung ownership polling times out and stale server evidence cannot look available', async ({ page, backend }) => {
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByText('Broker execution ownership · AVAILABLE', { exact: true }).click();
  // Leave the route unfulfilled to exercise a connection that never resolves.
  const hang = () => {};
  await page.route('**/api/execution-ownership', hang);
  await expect(page.getByText('Broker execution ownership · UNKNOWN', { exact: true })).toBeVisible({ timeout: 12000 });
  await expect(page.getByText(/Ownership request timed out after 5 seconds/)).toBeVisible();
  await expect(page.getByText(/No local runner currently holds/)).toHaveCount(0);
  await page.unroute('**/api/execution-ownership', hang);
  backend.ownership = { status: 'AVAILABLE', active: false, as_of: '2020-01-01T00:00:00Z' };
  await page.getByRole('button', { name: 'Retry ownership status', exact: true }).click();
  await expect(page.getByText(/Ownership reading expired or has an invalid timestamp/)).toBeVisible();
  await expect(page.getByText('Broker execution ownership · UNKNOWN', { exact: true })).toBeVisible();
  await expect(page.getByText(/No local runner currently holds/)).toHaveCount(0);
});
