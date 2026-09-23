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

test('paper params upload validates JSON, starts and stops the registered session', async ({ page, backend }) => {
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByRole('button', { name: 'Session controls' }).click();
  const file = page.locator('.action-panel__file-input[accept*="json"]');
  await file.setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{broken') });
  await expect(page.locator('.action-panel__error')).toBeVisible();
  await file.setInputFiles({ name: 'params.json', mimeType: 'application/json', buffer: Buffer.from('{"entry_threshold":0.002}') });
  await expect(page.getByText('params.json', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Start Paper Trading', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop Paper Trading', exact: true })).toBeVisible();
  expect(JSON.stringify(backend.requests.find(r => r.path === '/api/jobs/paper').body)).toContain('0.002');
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
