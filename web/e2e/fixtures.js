import { test as base, expect } from '@playwright/test';

export const preflight = {
  execution_eligible: true, errors: [], warnings: ['Synthetic browser fixture; not market evidence.'],
  sha256: 'a'.repeat(64), tickers: [{ ticker: 'QQQ', bars: 300, start: '2025-01-02', end: '2026-01-02', cadence_minutes: 240,
    zero_volume_bars: 0, missing_volume_bars: 0, invalid_volume_bars: 0, source: 'Browser fixture', session: 'RTH', price_basis: 'split_adjusted', volume_basis: 'shares' }],
};
export const test = base.extend({
  backend: async ({ page }, use) => {
    const state = { jobs: [], runs: [], campaigns: [], details: {}, commands: [], requests: [], failures: {}, unexpected: [],
      preflight: structuredClone(preflight), heartbeat: true, blockers: [] };
    await page.route('**/api/**', async route => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      const method = req.method();
      if (method === 'OPTIONS') return route.fulfill({ status: 204 });
      let body;
      try { body = req.postDataJSON(); } catch { body = req.postData(); }
      state.requests.push({ path, method, body });
      const reply = (json, status = 200) => route.fulfill({ status, json });
      if (state.failures[path]) return reply({ detail: 'Injected API failure' }, state.failures[path]);
      if (path === '/api/health') return reply({ status: 'ok', job_registry: { status: 'ready' } });
      if (path === '/api/profiles') return reply([]);
      if (path === '/api/readiness/live') return reply({ live_capital_enabled: false, code: 'READINESS_BLOCKED', gates: [] });
      if (path === '/api/broker/config') return reply({ status: 'disconnected' });
      if (path === '/api/broker/status') return reply({ status: 'disconnected', message: 'Isolated browser fixture' });
      if (path.startsWith('/api/broker/bars')) return reply({ bars: [], status: 'unavailable' });
      if (path === '/api/live/news') return reply({ items: [], articles: [], status: 'unavailable' });
      if (path === '/api/live/telemetry') return reply({ mock: false, job_id: 'paper-test', status: 'running', job_status: 'running', as_of: new Date().toISOString(), tickers: ['QQQ'], series: {}, risk: {}, model: {} });
      if (path === '/api/runs') return reply(state.runs);
      if (path.match(/^\/api\/runs\/[^/]+\/research$/)) return reply({ benchmark: { status: 'unavailable', message: 'No benchmark fixture' }, strategy: {}, relative: {} });
      if (path.startsWith('/api/runs/')) return reply(state.runs.find(r => r.run_id === path.split('/')[3]));
      if (path === '/api/campaigns') return reply(state.campaigns);
      if (path.startsWith('/api/campaigns/')) return reply(state.details[path.split('/').at(-1)] || {}, state.details[path.split('/').at(-1)] ? 200 : 404);
      if (path === '/api/jobs/data/preflight') return reply(state.preflight);
      if (path === '/api/jobs/upload-csv') return reply({ path: 'quant/jobs/uploads/browser.csv', filename: 'browser.csv' });
      if (path === '/api/jobs') return reply(state.jobs);
      if (method === 'POST' && /^\/api\/jobs\/(backtest|optimize|paper|data\/repair|campaign\/\w+)$/.test(path)) {
        const kind = path.slice('/api/jobs/'.length).replace('data/repair', 'data_repair').replace('/', '_');
        const job = { id: `${kind}-test`, kind, status: 'running', started_at: new Date().toISOString(), config: body,
          run_id: ['backtest', 'optimize'].includes(kind) ? `${kind}-result` : null };
        state.jobs.unshift(job);
        return reply(job);
      }
      if (/^\/api\/jobs\/[^/]+\/(logs|progress)$/.test(path)) return reply(path.endsWith('/logs') ? { lines: ['Browser fixture process output'] } : { percent: 40, phase: 'replay', phase_label: 'Fixture computation', report: state.preflight });
      if (/^\/api\/jobs\/[^/]+\/cancel$/.test(path)) {
        const job = state.jobs.find(j => j.id === path.split('/')[3]);
        job.status = 'cancelling'; return reply(job);
      }
      if (/^\/api\/jobs\/[^/]+$/.test(path)) return reply(state.jobs.find(j => j.id === path.split('/')[3]));
      if (path === '/api/operations/paper-test') return reply({ job_id: 'paper-test', operator: 'Browser test', heartbeat_fresh: state.heartbeat, resume_blockers: state.blockers, commands: state.commands });
      if (path === '/api/operations/paper-test/commands') {
        let command = state.commands.find(c => c.request_id === body.request_id);
        if (!command) { command = { ...body, command_id: 'command-test', status: 'PENDING', requested_at: new Date().toISOString() }; state.commands.push(command); }
        return reply(command);
      }
      if (path.endsWith('/commands/command-test/cancel')) { state.commands[0].status = 'CANCELLED'; return reply(state.commands[0]); }
      state.unexpected.push(`${method} ${path}`);
      return reply({ detail: `Unexpected browser test request: ${method} ${path}` }, 501);
    });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await use(state);
    expect(state.unexpected, 'All API requests must be isolated and declared').toEqual([]);
    expect(errors, 'No uncaught browser errors').toEqual([]);
  },
});
export { expect };
export async function configure(page, workflow = 'backtest') {
  await page.goto(`/?asset=equity&workflow=${workflow}`);
  await page.getByRole('button', { name: workflow === 'optimize' ? /New sweep/ : /New backtest/ }).click();
  await expect(page.getByText('Data preflight passed', { exact: true })).toBeVisible();
}
