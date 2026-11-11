import { test, expect } from './fixtures.js';
async function unlock(page, backend) {
  backend.jobs = [{ id: 'paper-test', kind: 'paper', status: 'running', config: { asset_class: 'equity', tickers: ['QQQ'] } }];
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByText('Operator safety controls', { exact: true }).click();
  await page.getByLabel('Operator token', { exact: true }).fill('isolated-test-token');
  await page.getByRole('button', { name: 'Unlock controls' }).click();
  await expect(page.getByText('Browser test · strategy responding')).toBeVisible();
}

test('paper safety command review, cancellation and credentials cleared on reload', async ({ page, backend }) => {
  await unlock(page, backend);
  await page.getByLabel('Reason', { exact: true }).fill('Browser fixture freeze');
  await page.getByRole('button', { name: 'Freeze entries', exact: true }).click();
  await expect(page.getByText('Waiting for the strategy to claim this request.')).toBeVisible();
  await page.getByRole('button', { name: 'Cancel pending request' }).click();
  await expect(page.getByText('Cancelled before the strategy claimed the request.')).toBeVisible();
  await page.reload();
  await page.getByText('Operator safety controls', { exact: true }).click();
  await expect(page.getByLabel('Operator token', { exact: true })).toHaveValue('');
});

test('stale heartbeat and API failure cannot enable resume', async ({ page, backend }) => {
  await unlock(page, backend);
  await page.getByRole('combobox', { name: 'Action', exact: true }).selectOption('RESUME_ENTRIES');
  await page.getByLabel('Reason', { exact: true }).fill('Resume fixture');
  await page.getByLabel('Type RESUME_ENTRIES strategy:paper-test').fill('RESUME_ENTRIES strategy:paper-test');
  backend.heartbeat = false;
  await expect(page.getByText('Browser test · strategy heartbeat stale')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Resume entries', exact: true })).toBeDisabled();
  backend.failures['/api/operations/paper-test'] = 503;
  await expect(page.getByText('Control status unknown')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Resume entries', exact: true })).toBeDisabled();
});

test('uncertain command receipt retries the same identity, without duplicate commands', async ({ page, backend }) => {
  await unlock(page, backend);
  let first = true;
  await page.route('**/api/operations/paper-test/commands', async route => {
    if (first) { first = false; return route.fulfill({ status: 503, json: { detail: 'Receipt uncertain' } }); }
    return route.fallback();
  });
  const requests = [];
  page.on('request', req => { if (new URL(req.url()).pathname.endsWith('/paper-test/commands')) requests.push(req.postDataJSON()); });
  await page.getByLabel('Reason', { exact: true }).fill('Freeze fixture');
  await page.getByRole('button', { name: 'Freeze entries', exact: true }).click();
  await page.getByRole('button', { name: 'Retry request', exact: true }).click();
  await expect(page.getByText('Waiting for the strategy to claim this request.')).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(backend.commands).toHaveLength(1);
});

test('paper selects an Optuna result, starts and stops the registered session', async ({ page, backend }) => {
  backend.runs = [{ run_id: 'optuna-test', kind: 'optimize', asset_class: 'equity', allow_short_positions: false, tickers: ['QQQ'] }];
  backend.details['optuna-test'] = { ...backend.runs[0], best_params: { entry_threshold: 0.002 }, bar_interval_minutes: 240 };
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByRole('button', { name: 'Session controls' }).click();
  await expect(page.locator('input[type=file][accept*="json"]')).toHaveCount(1); // sector evidence only
  await expect(page.getByRole('button', { name: 'Start Paper Trading', exact: true })).toBeDisabled();
  await page.getByRole('combobox', { name: 'Optuna result', exact: true }).selectOption('optuna-test');
  await page.getByRole('button', { name: 'Start Paper Trading', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop Paper Trading', exact: true })).toBeVisible();
  const body = backend.requests.find(r => r.path === '/api/jobs/paper').body;
  expect(body.source_run_id).toBe('optuna-test');
  expect(body).not.toHaveProperty('params');
  expect(body).not.toHaveProperty('params_path');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Stop Paper Trading', exact: true }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
});

for (const [action, label] of [['RESUME_ENTRIES', 'Resume entries'], ['CANCEL_ALL', 'Cancel orders · flat book only'], ['FLATTEN', 'Flatten positions'], ['KILL', 'Engage permanent kill']]) {
  test(`${action} requires exact action and target confirmation`, async ({ page, backend }) => {
    await unlock(page, backend);
    await page.getByRole('combobox', { name: 'Action', exact: true }).selectOption(action);
    await page.getByLabel('Reason', { exact: true }).fill('Browser safety fixture');
    await expect(page.getByRole('button', { name: label, exact: true })).toBeDisabled();
    await page.getByLabel(`Type ${action} strategy:paper-test`).fill(`${action} strategy:paper-test`);
    await page.getByRole('button', { name: label, exact: true }).click();
    await expect(page.getByText('Waiting for the strategy to claim this request.')).toBeVisible();
    expect(backend.commands[0]).toMatchObject({ action, confirmation: `${action} strategy:paper-test` });
  });
}

test('live capital stays locked when readiness is unavailable', async ({ page, backend }) => {
  backend.failures['/api/readiness/live'] = 503;
  await page.goto('/?asset=equity&workflow=live');
  await page.getByRole('button', { name: 'Session controls' }).click();
  await expect(page.getByText(/Readiness status unavailable/)).toBeVisible();
  await expect(page.locator('.action-panel__actions .button-danger')).toBeDisabled();
  expect(backend.requests.some(r => r.path === '/api/jobs/live')).toBe(false);
});


test('paper configuration follows the connected Gateway port and preserves a manual edit', async ({ page, backend }) => {
  await page.addInitScript(() => localStorage.setItem('quant-dashboard.action-settings.v1', JSON.stringify({ port: 7497 })));
  await page.route('**/api/broker/status', route => route.fulfill({ json: {
    status: 'connected', config: { host: '127.0.0.1', port: 4002, mode: 'paper' },
  } }));
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByRole('button', { name: 'Session controls' }).click();
  await page.locator('.action-panel__trading-advanced > summary').filter({ hasText: /^Advanced$/ }).click();
  const port = page.getByLabel('Port', { exact: true });
  await expect(port).toHaveValue('4002');
  await port.fill('7497');
  await expect(port).toHaveValue('7497');
});
