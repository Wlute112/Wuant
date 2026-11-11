import { test, expect } from './fixtures.js';

for (const width of [1440, 390]) {
  test(`daily chart shows historical bars despite missing live permission at ${width}px`, async ({ page, backend }) => {
    const snapshot = { source: 'ib_gateway', status: 'historical', symbol: 'QQQ', asset_class: 'equity',
      bar_hours: 24, update_mode: 'historical_snapshot', as_of: '2026-11-09T01:00:00Z', error: null,
      warning: 'Live updates unavailable with the current market-data permissions.', bars: [
        { ts: '2026-11-05T00:00:00Z', session_date: '2026-11-05', open: 100, high: 103, low: 99, close: 102, volume: 1000, complete: true },
        { ts: '2026-11-06T00:00:00Z', session_date: '2026-11-06', open: 102, high: 105, low: 101, close: 104, volume: 1000, complete: true },
      ] };
    await page.route('**/api/broker/bars**', route => route.fulfill({ json: snapshot }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?asset=equity&workflow=paper');
    await page.getByRole('combobox', { name: 'Bars', exact: true }).selectOption('24');
    await expect(page.getByRole('status').filter({ hasText: 'Historical snapshot' })).toBeVisible();
    await expect(page.getByText('Live updates unavailable with the current market-data permissions.')).toBeVisible();
    await expect(page.locator('.live-panel__market-error')).toHaveCount(0);
    await expect(page.getByText(/Historical market tape/)).toBeVisible();
    await expect(page.getByRole('img', { name: /QQQ candlestick chart/ })).toBeVisible();
    await page.screenshot({ path: `test-results/history-chart-${width}.png`, fullPage: true });
  });
}
