import { test, expect } from './fixtures.js';

const campaign = { campaign_id: 'equity_fixture', asset_class: 'equity', studies_complete: 4, studies_total: 4,
  comparison_ready: true, robustness_ready: true, promotion_status: 'UNTOUCHED',
  manifest: { validation_contract: { csv_sha256: 'locked-fixture-hash', seeds: [42,43,44,45] } },
  comparison: { consensus: 'Fixture cluster' }, robustness: { passed: true }, promoted_params: null };
async function open(page, backend, detail = campaign) {
  backend.campaigns = [detail]; backend.details[detail.campaign_id] = structuredClone(detail);
  await page.goto('/?asset=equity&workflow=optimize');
  await page.getByRole('button', { name: 'Optimization', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Build consensus' })).toBeEnabled();
}

test('campaign evidence review and stage launch, progress and cancellation', async ({ page, backend }) => {
  await open(page, backend);
  await page.getByText('Locked validation contract', { exact: true }).click();
  await expect(page.locator('pre').filter({ hasText: 'locked-fixture-hash' })).toBeVisible();
  await page.screenshot({ path: 'test-results/campaign-review.png', fullPage: true });
  await page.getByRole('button', { name: 'Build consensus' }).click();
  await page.getByText('campaign_compare · running · progress & logs', { exact: true }).click();
  await expect(page.getByText('Running · Fixture computation', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  expect(backend.jobs[0].status).toBe('cancelling');
});

test('consumed holdout and unavailable evidence lock all mutating stages', async ({ page, backend }) => {
  await open(page, backend);
  backend.details.equity_fixture.promotion_status = 'CONSUMED_FAILED';
  await page.getByRole('button', { name: 'Reload campaign evidence' }).click();
  await expect(page.getByText('CONSUMED_FAILED', { exact: true })).toBeVisible();
  await page.getByLabel('Outer holdout confirmation phrase').fill('CONSUME OUTER HOLDOUT');
  await expect(page.getByRole('button', { name: 'Evaluate outer holdout' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Build consensus' })).toBeDisabled();
  backend.failures['/api/campaigns'] = 503;
  await page.getByRole('button', { name: 'Reload campaign evidence' }).click();
  await expect(page.getByText('UNKNOWN', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run robustness suite' })).toBeDisabled();
});

for (const [button, path] of [['Run robustness suite', 'robustness'], ['Evaluate outer holdout', 'promote']]) {
  test(`campaign ${path} forwards locked target and confirmation`, async ({ page, backend }) => {
    await open(page, backend);
    if (path === 'promote') await page.getByLabel('Outer holdout confirmation phrase').fill('CONSUME OUTER HOLDOUT');
    await page.getByRole('button', { name: button }).click();
    const request = backend.requests.find(r => r.path === `/api/jobs/campaign/${path}`);
    expect(request.body.campaign_id).toBe('equity_fixture');
    if (path === 'promote') expect(request.body.confirm).toBe('CONSUME OUTER HOLDOUT');
  });
}

test('multi-seed configuration launches from preflight', async ({ page, backend }) => {
  await open(page, backend);
  await page.getByLabel('Campaign ID', { exact: true }).fill('equity_new');
  await page.getByLabel('Independent seeds').fill('51 52 53');
  await page.getByRole('button', { name: 'Start multi-seed campaign' }).click();
  expect(backend.requests.find(r => r.path === '/api/jobs/campaign/seeds').body).toMatchObject({ campaign_id: 'equity_new', seeds: [51,52,53] });
});
