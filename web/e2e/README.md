# Dashboard browser integration

From `quant/web`:

```sh
npm ci
npx playwright install chromium
npm run test:e2e
```

To use an installed Google Chrome instead of downloading Chromium:

```sh
PLAYWRIGHT_CHANNEL=chrome npm run test:e2e
```

Playwright starts a dedicated Vite server on port 4179 and opens fresh browser
contexts. Every `/api/` request is intercepted by the in-memory fixture server;
unrecognized API requests and uncaught browser errors fail the test. No running
Quant API, Redis, IBKR session, research datasets, or operator credentials are
used. Mutation assertions inspect actual browser HTTP payloads. API authorization,
validation, persistence and idempotency are independently covered by
`tests/test_api_jobs.py`, `tests/test_operations_api.py`, and
`tests/test_research_hub.py` (run with the repository parent on `PYTHONPATH`).

The suite covers preflight rejection and recovery, CSV import, repair settings,
progress/logs/cancellation, backtest and optimization launch/reload/result review,
calibration evidence, multi-seed/consensus/robustness/holdout flows, consumed and
unknown holdout interlocks, paper params and session cancellation, authenticated
safety controls, uncertain-request retries, stale heartbeat, API failure, live
readiness lock, repeated launch clicks, and narrow-screen keyboard/focus recovery.
All data and command outcomes are labeled browser fixtures. This proves browser
workflow behavior; it does not prove historical calibration, broker compatibility,
actual fills, paper soak performance, or any P0 approval.

Failures retain screenshots and traces in `test-results/`; the HTML report is in
`playwright-report/`. Open it with `npx playwright show-report`. CI installs pinned
npm dependencies and the matching Chromium build, runs unit tests, production
build and browser tests, and retains reports for 14 days.
