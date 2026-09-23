import { test, expect } from './fixtures.js';

const config = { repository: 'sftp:backup@fixture.example:/quant', password_file: '/private/fixture/password', retain: 56, rpo_hours: 6, rto_minutes: 120, scheduled: false, redis_host: '127.0.0.1', redis_port: 6379 };

async function setup(page) {
  const state = { config: null, reports: [], dependencies: { restic: true, 'redis-cli': true, 'redis-check-rdb': true },
    backup_age_hours: null, rpo_status: 'unknown', as_of: new Date().toISOString(), inventory: { snapshots: [{ id: 'a'.repeat(64), time: '2026-09-13T00:00:00Z' }] } };
  const requests = [];
  let fail = false;
  await page.route('**/api/recovery{,/**}', async route => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() === 'GET') return route.fulfill({ status: fail ? 503 : 200, json: fail ? { detail: 'Fixture offline' } : state });
    const body = req.postDataJSON();
    requests.push({ path, body });
    if (path.endsWith('/config')) { state.config = body; return route.fulfill({ json: body }); }
    if (path.endsWith('/cancel')) { state.reports[0].status = 'cancelling'; return route.fulfill({ json: { status: 'cancelling' } }); }
    const report = { id: body.request_id.replaceAll('-', ''), action: body.action, status: 'running', phase: 'Fixture encrypted transfer', job_id: `recovery_${body.request_id}` };
    state.reports = [report];
    return route.fulfill({ json: { id: report.job_id, status: 'running' } });
  });
  await page.goto('/?asset=equity&workflow=paper');
  await page.getByText('Backup & recovery', { exact: true }).click();
  await page.getByLabel('Recovery operator token').fill('fixture-token');
  await page.getByRole('button', { name: 'Unlock recovery controls' }).click();
  await expect(page.getByLabel('SFTP repository')).toBeVisible();
  return { state, requests, fail: () => { fail = true; } };
}

test('recovery configuration, launch, cancellation and isolated restore evidence', async ({ page, backend }) => {
  const { state, requests } = await setup(page);
  await page.getByLabel('SFTP repository').fill(config.repository);
  await page.getByLabel('Repository password file on API host').fill(config.password_file);
  await expect(page.getByRole('button', { name: 'Launch recovery job' })).toBeDisabled();
  await page.getByRole('button', { name: 'Save recovery configuration' }).click();
  await page.getByRole('button', { name: 'Launch recovery job' }).click();
  await expect(page.getByRole('button', { name: 'Cancel recovery job' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel recovery job' }).click();
  state.reports[0].status = 'cancelled';
  await page.getByLabel('Recovery action').selectOption('restore');
  await page.getByLabel('Remote snapshot').selectOption('a'.repeat(64));
  await expect(page.getByRole('button', { name: 'Launch recovery job' })).toBeDisabled();
  await page.getByLabel(`Type RESTORE ISOLATED ${'a'.repeat(64)}`).fill(`RESTORE ISOLATED ${'a'.repeat(64)}`);
  await page.getByRole('button', { name: 'Launch recovery job' }).click();
  state.reports[0] = { ...state.reports[0], status: 'completed', restore_path: '/isolated/fixture/bundle', activation_allowed: false, different_hostname: false };
  await expect(page.getByText('Same-host drill only', { exact: false })).toBeVisible();
  await page.getByText('Review recovery evidence', { exact: true }).click();
  await expect(page.locator('.recovery-controls pre')).toContainText('"activation_allowed": false');
  expect(requests.filter(r => r.path === '/api/recovery/jobs')).toHaveLength(2);
  await page.screenshot({ path: 'test-results/recovery-desktop.png', fullPage: true });
});

test('narrow recovery preserves draft on network failure and clears credentials on reload', async ({ page, backend }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const server = await setup(page);
  await page.getByLabel('SFTP repository').fill(config.repository);
  server.fail();
  await expect(page.getByText('Status unknown — controls locked')).toBeVisible();
  await expect(page.getByLabel('SFTP repository')).toHaveValue(config.repository);
  await expect(page.getByRole('button', { name: 'Save recovery configuration' })).toBeDisabled();
  await page.screenshot({ path: 'test-results/recovery-mobile.png', fullPage: true });
  await page.reload();
  await page.getByText('Backup & recovery', { exact: true }).click();
  await expect(page.getByLabel('Recovery operator token')).toHaveValue('');
});

test('uncertain recovery launch retries the same identity and cannot lock during submission', async ({ page, backend }) => {
  const { state } = await setup(page);
  state.config = config;
  await page.getByRole('button', { name: 'Lock controls', exact: true }).click();
  await page.getByLabel('Recovery operator token').fill('fixture-token');
  await page.getByRole('button', { name: 'Unlock recovery controls' }).click();
  const bodies = [];
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  await page.route('**/api/recovery/jobs', async route => {
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) {
      await delayed;
      return route.fulfill({ status: 503, json: { detail: 'Uncertain receipt fixture' } });
    }
    return route.fallback();
  });
  await page.getByRole('button', { name: 'Launch recovery job' }).click();
  await expect(page.getByRole('button', { name: 'Lock controls', exact: true })).toBeDisabled();
  release();
  await page.getByRole('button', { name: 'Retry same recovery request' }).click();
  await expect(page.getByRole('button', { name: 'Cancel recovery job' })).toBeVisible();
  expect(bodies).toHaveLength(2);
  expect(bodies[0]).toEqual(bodies[1]);
});
