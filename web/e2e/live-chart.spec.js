import { test, expect } from './fixtures.js';

test('chart controls, layers, presets and settings survive reload', async ({ page, backend }) => {
  const bars = Array.from({ length: 240 }, (_, index) => ({
    ts: new Date(Date.UTC(2026, 0, 1) + index * 3600000).toISOString(),
    open: 100 + index / 10, high: 102 + index / 10, low: 99 + index / 10, close: 101 + index / 10,
  }));
  await page.route('**/api/broker/bars**', route => route.fulfill({ json: { bars, source: 'ib_gateway', status: 'historical', update_mode: 'historical_snapshot' } }));
  await page.goto('/?asset=equity&workflow=paper');
  const chart = page.getByRole('region', { name: 'Chart workspace', exact: true });
  await expect(chart.getByRole('button', { name: 'Following latest' })).toBeVisible();
  await chart.getByRole('button', { name: 'Earlier', exact: true }).click();
  await expect(chart.getByRole('button', { name: 'Follow latest', exact: true })).toBeVisible();
  await page.waitForTimeout(2500);
  await expect(chart.getByRole('button', { name: 'Follow latest', exact: true })).toHaveAttribute('aria-pressed', 'false');
  await chart.getByRole('button', { name: 'Follow latest', exact: true }).click();
  await chart.getByRole('combobox', { name: 'Chart type' }).selectOption('line');
  await chart.getByRole('button', { name: 'Settings', exact: true }).click();
  await chart.getByRole('checkbox', { name: 'Show grid' }).uncheck();
  await chart.getByLabel('Preset name').fill('My monitor');
  await chart.getByRole('button', { name: 'Save preset', exact: true }).click();
  await page.keyboard.press('Escape');
  await page.reload();
  await expect(chart.getByRole('combobox', { name: 'Chart type' })).toHaveValue('line');
  await chart.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(chart.getByRole('checkbox', { name: 'Show grid' })).not.toBeChecked();
  await expect(chart.getByRole('button', { name: 'Delete My monitor' })).toBeVisible();
  await page.keyboard.press('Escape');
  await chart.getByRole('button', { name: 'Expand chart' }).click();
  await expect(chart).toHaveClass(/is-expanded/);
  await page.keyboard.press('Escape');
  await expect(chart).not.toHaveClass(/is-expanded/);
});
