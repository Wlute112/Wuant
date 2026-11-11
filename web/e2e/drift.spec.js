import { test, expect } from './fixtures.js';

const VERSION = 'a'.repeat(64);
const metric = (status = 'breach') => ({
  baseline: { n: 40, mean: 0.01, ci95: [0.008, 0.012], status: 'ready' },
  current: { n: 35, mean: -0.02, ci95: [-0.024, -0.016], status: 'ready' },
  delta: -0.03, threshold: 0.002, status,
});

async function reviewBackend(page, backend) {
  const state = { sources: [], baselines: [], reports: [], alerts: [], config: null,
    as_of: new Date().toISOString(), scheduler: 'Every 15 seconds while the API is running',
    limitations: 'Browser fixture; not market evidence. Intervals are descriptive. No automatic model changes.', requests: [], fail: false };
  backend.runs = [{ run_id: 'bt-review', name: 'QQQ review fixture', kind: 'backtest', asset_class: 'equity' }];
  await page.route('**/api/drift**', async route => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const body = req.postData() ? req.postDataJSON() : null;
    state.requests.push({ path, body, auth: req.headers().authorization });
    if (state.fail) return route.fulfill({ status: 503, json: { detail: 'Review database unavailable' } });
    const reply = json => route.fulfill({ json });
    if (req.method() === 'GET') return reply(state);
    if (path === '/api/drift/research/bt-review') {
      state.sources.push({ source_id: 'bt-review', kind: 'research', sample_count: 40, contract: { model_version: VERSION, asset_class: 'equity' }, as_of: state.as_of, reference: 'Deterministic research browser fixture' });
      return reply(state.sources.at(-1));
    }
    if (path === '/api/drift/evidence') {
      state.sources.push({ ...body, sample_count: 35 });
      return reply(body);
    }
    if (path === '/api/drift/baselines') {
      const baseline = { ...body, id: 'baseline-test', recorded_at: state.as_of, model_version: VERSION };
      state.baselines.push(baseline); return reply(baseline);
    }
    if (path === '/api/drift/config') {
      state.config = { ...body, id: 'settings-test' }; return reply(state.config);
    }
    if (path === '/api/drift/review') {
      state.reports = [{ id: 'report-test', config_id: state.config.id, model_version: VERSION, recorded_at: state.as_of,
        stage_coverage: { research: 0, shadow: 1, paper: 0 }, source_errors: [],
        sources: [{ source_id: 'shadow-review', kind: 'shadow', status: 'reviewed', as_of: state.as_of,
          sha256: VERSION, reference: 'Recorded shadow browser fixture', groups: {
            All: { prediction_mean: metric(), slippage_bps: { ...metric('insufficient'), current: { n: 0, mean: null, ci95: null } } },
            Bull: { prediction_mean: metric('insufficient') },
          } }] }];
      state.alerts = [{ id: 'alert-test', key: 'shadow-review/All/prediction_mean', status: 'breach', recorded_at: state.as_of }];
      return reply(state.reports[0]);
    }
    if (path === '/api/drift/stop') {
      state.config = { ...state.config, enabled: false, id: 'paused-test' }; return reply(state.config);
    }
    return route.fulfill({ status: 404, json: { detail: `Unexpected drift path ${path}` } });
  });
  return state;
}

for (const [width, theme] of [[1440, 'dark'], [768, 'light'], [390, 'lilac']]) {
  test(`drift workflow configures reviews and shows honest evidence at ${width}px`, async ({ page, backend }) => {
    const state = await reviewBackend(page, backend);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/?workflow=review&asset=equity');
    await expect(page.getByRole('heading', { name: 'Drift & execution review' })).toBeVisible();
    await page.getByLabel('Theme', { exact: true }).selectOption(theme);
    await page.getByLabel('Review operator token').fill('browser-fixture-token');
    await page.getByRole('button', { name: 'Unlock drift review' }).click();
    await expect(page.getByText('No evidence imported.', { exact: false })).toBeVisible();
    await page.getByLabel('Backtest run').selectOption('bt-review');
    await page.getByRole('button', { name: 'Import research evidence' }).click();
    await expect(page.getByText('Backtest evidence imported.', { exact: true })).toBeVisible();
    await page.getByText('Import recorded evidence JSON', { exact: true }).click();
    await page.getByLabel('Evidence JSON').fill(JSON.stringify({ source_id: 'shadow-review', kind: 'shadow',
      contract: { model_version: VERSION, asset_class: 'equity' }, as_of: state.as_of, reference: 'Recorded shadow browser fixture', samples: [] }));
    await page.getByRole('button', { name: 'Import recorded evidence', exact: true }).click();
    await expect(page.getByText('Recorded evidence imported.', { exact: true })).toBeVisible();
    await page.getByLabel('Baseline source', { exact: true }).selectOption('bt-review');
    await page.getByLabel('Baseline review reason').fill('Reviewed research fixture');
    await page.getByRole('button', { name: 'Capture baseline' }).click();
    await expect(page.getByLabel('Reviewed baseline', { exact: true })).toHaveValue('baseline-test');
    await page.getByLabel('Settings review reason').fill('Reviewed threshold fixture');
    await page.getByLabel('Minimum samples per metric').fill('35');
    await page.getByRole('button', { name: 'Save review settings' }).click();
    await expect(page.getByRole('button', { name: 'Run review now' })).toBeEnabled();
    expect(state.config.minimum_samples).toBe(35);
    expect(state.config.thresholds.prediction_mean).toBe(0.002);
    await page.getByRole('button', { name: 'Run review now' }).click();
    const results = page.getByRole('region', { name: 'Comparison results', exact: true });
    await expect(results.getByText(/paper: comparison unavailable/)).toBeVisible();
    const table = page.getByRole('region', { name: 'shadow-review All metric comparison', exact: true });
    await expect(table).toContainText('n = 35');
    await expect(table).toContainText('breach');
    await expect(table).toContainText('insufficient');
    await expect(table).toContainText('Unavailable');
    await expect(page.getByRole('region', { name: 'Alert history', exact: true })).toContainText('shadow-review/All/prediction_mean');
    await results.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/drift-${width}-${theme}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const contrast = await page.locator('.drift-review').evaluate(el => {
      const style = getComputedStyle(el);
      const luminance = name => {
        const color = style.getPropertyValue(name).trim();
        const rgb = color.startsWith('#') ? color.slice(1).match(/../g).map(v => parseInt(v, 16)) : color.match(/[\d.]+/g).slice(0, 3).map(Number);
        const linear = rgb.map(v => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
      };
      const a = luminance('--color-text-primary'), b = luminance('--color-panel-raised');
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
    // Exercise mouse scrolling before switching Chromium into touch emulation.
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.mouse.move(2, 400);
    await page.mouse.wheel(0, 600);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    if (width === 390) {
      await table.scrollIntoViewIfNeeded();
      const bounds = await table.boundingBox();
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true });
      const x = bounds.x + bounds.width - 20, y = bounds.y + Math.min(bounds.height / 2, 100);
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
      for (let distance = 20; distance <= 180; distance += 20) {
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x - distance, y }] });
      }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await expect.poll(() => table.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: false });
      await cdp.detach();
    }
    // Inputs remain keyboard-operable and have visible focus in each theme.
    await page.getByLabel('Settings review reason').focus();
    await page.keyboard.press('Tab');
    await expect(page.getByLabel('Minimum samples per metric')).toBeFocused();
    expect(await page.getByLabel('Minimum samples per metric').evaluate(el => getComputedStyle(el).outlineStyle)).toBe('solid');
    await page.getByRole('button', { name: 'Pause automatic review' }).click();
    await expect(page.getByText('Automatic review paused', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Run review now' })).toBeDisabled();
    await expect(results.getByText(/Historical result/)).toBeVisible();
    await page.getByLabel('Recorded review', { exact: true }).selectOption('report-test');
    await expect(table).toContainText('n = 35');
    expect(state.requests.every(r => r.auth === 'Bearer browser-fixture-token')).toBe(true);
  });
}

test('drift review retains unsaved settings through a network interruption and locks on authentication failure', async ({ page, backend }) => {
  const state = await reviewBackend(page, backend);
  await page.goto('/?workflow=review');
  await page.getByLabel('Review operator token').fill('browser-fixture-token');
  await page.getByRole('button', { name: 'Unlock drift review' }).click();
  await page.getByLabel('Settings review reason').fill('Keep this unsaved reason');
  state.fail = true;
  await expect(page.getByRole('alert')).toContainText('Review database unavailable', { timeout: 10000 });
  await expect(page.getByRole('button', { name: 'Capture baseline' })).toBeDisabled();
  await expect(page.getByLabel('Settings review reason')).toHaveValue('Keep this unsaved reason');
  state.fail = false;
  await expect(page.getByRole('alert')).toHaveCount(0, { timeout: 10000 });
  await expect(page.getByLabel('Settings review reason')).toHaveValue('Keep this unsaved reason');
  await page.getByRole('button', { name: 'Lock review', exact: true }).click();
  await expect(page.getByLabel('Review operator token')).toHaveValue('');
});
