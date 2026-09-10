# Product & Production Readiness

This is the single prioritized backlog for the product. Last consolidated:
2026-09-07. It incorporates remaining work from `AGENTS.md`, `CLAUDE.md`,
this readiness register, and the hardware-validation follow-up in
`MAC_STUDIO_SCALING.md`. Product, design, news and operations documents retain
their specifications and procedures; future work is tracked here.

Work is ordered by descending importance: P0 capital safety, P1 trustworthy
research and deployment evidence, P2 operational and product improvements, then
P3 optional research extensions. Within each table, higher rows take precedence;
dependencies may determine execution order. **Proposed** rows are recommended
additions, not claims of a verified defect or commitments to expand scope.

Live-capital deployment is fail-closed. `quant.run.readiness` is the canonical
gate, the CLI and TradingNode builder enforce it before adapter startup, the API
returns HTTP 503, and the dashboard keeps live controls locked. There is no
environment-variable bypass.

Paper and live use the same `build_node` and `MLStrategy` execution path. Mode
changes only the broker account/port and whether capital permission can pass the
readiness gate. IBKR paper execution is restricted to US equities and ETFs;
shorts are explicit opt-in and fail closed when any required broker control is
unavailable. Spot-crypto paper execution is unavailable at IBKR.

## Status vocabulary

- **Implemented, validation pending**: a fail-closed implementation and local
  deterministic tests exist, but supported TWS/Gateway compatibility and paper
  behavior have not been proven.
- **Open**: material implementation work remains.
- **Proposed**: recommended improvement; confirm the implementation gap and scope
  when starting the item.
- **Deferred**: outside the supported product scope; requires a separate design
  and validation contract before implementation.
- **Promotion gate**: evidence must be recorded before the readiness flag can be
  reviewed; code presence alone is insufficient.

## P0 gate register

| Gate | Current status | Implemented evidence | Required before approval |
|---|---|---|---|
| Broker source of truth | Implemented, validation pending | Broker-neutral all-account position/order/execution/account reconciliation; deterministic stale lifecycle/fill recovery; post-Nautilus reconciliation cache normalization; manual/foreign exposure detection; direct IBKR buying-power, available-funds and excess-liquidity fields; distinct account/currency-specific SettledCash with freshness, reconnect invalidation, audit provenance and dashboard review; immutable reports; unresolved state freezes execution | Prove IBKR snapshot-end ordering, permanent-ID/correction handling, all-client visibility and restart races on supported TWS/Gateway; validate SettledCash currency, settlement transitions, callback cadence and reconnect invalidation on supported cash and margin accounts |
| Real-time risk | Implemented, validation pending | One-second in-strategy checks plus an independently launched supervisor with a required heartbeat, durable freeze/flatten/kill commands, telemetry/data-age checks, alert delivery and bounded service watchdogs; broker equity, drawdown, leverage, daily loss, account availability, gross/symbol/order/concentration limits, price collars, direct IBKR margin/PDT state and order-specific what-if checks | Reviewed conId-bound stock/ETF sector exposure and explicit adapter disconnect callbacks are implemented; prove behavior against supported TWS/Gateway under sector breaches, disconnect and rejection faults |
| Kill-switch | Implemented, validation pending | Immediate entry freeze; cancel-confirm before emergency exits; fill confirmation; unresolved state remains disabled; structured operator alerts | Fault-injection paper tests for cancellation rejection, exit rejection, disconnect and residual positions |
| Broker protection | Implemented, validation pending | Actual-fill-based stop-market/take-profit OCA pair; partial-fill resize; adjustment replacement; restart reconciliation; authoritative telemetry distinguishes acknowledged OCA orders from model references | Verify IBKR OCA tags, transmit semantics, modification ordering, RTH/outside-RTH support, gap behavior and restart adoption against supported adapter/TWS versions |
| Order lifecycle | Implemented, validation pending | Idempotent order/fill ledger; submitted, acknowledged, partial, filled, canceled, expired, rejected and denied states; execution IDs, corrections, actual average fills, DAY equity entries, stale-entry cancellation, rejection suspension and alerts | Normalize and verify IBKR correction/permanent-ID callbacks; paper-test cancel/fill races, partial fills, stale replacement and every rejection class |
| Safe shutdown | Implemented, validation pending | Entry freeze; cancel-confirm stage; framework flatten-confirm stage; no signal resolution or submission in `on_stop`; orphan/residual detection | TWS tests for cancel timeout, disconnect during shutdown, optional keep/flatten policy, and broker orders owned by other clients |
| Equity short controls | Implemented, validation pending | US/USD exchange allowlist; TWS shortable tier/share, halt, NBBO and Rule-201 checks; IBKR fee/availability feed; configurable fee, locate and margin cushions; PDT state; order-specific what-if margin/commission approval; limit-only entries; broker-protected exits; persistent-breach cover; independent-supervisor freeze/flatten | Paper-test easy/hard-to-borrow names, fee changes, zero inventory, SSR days, cash and margin accounts, PDT exhaustion, locate rejection, forced broker reductions, restart and disconnect behavior against supported IBKR builds |
| Exchange sessions | Implemented, validation pending | IBKR TradingHours/LiquidHours/timeZoneId parser; holidays, early closes, DST, overnight windows, phases, explicit halts and stale-data states | Capture and validate real contract-details variants; model exchange-wide unexpected closures and auction/halts using supported IBKR callbacks |
| Session policies | Implemented, validation pending | Shared validated CLI/API/dashboard policy; RTH, extended and custom windows; open/close buffers; auction flags; no-entry-before-close; resting-entry cancellation at each configured window end, including RTH close and custom breaks; policy and entry decisions in telemetry | Paper-test daily after-close signals, cancellation acknowledgements, custom-window boundaries and each supported order type |
| Session risk accounting | Implemented, validation pending | Configurable prior/next-session PnL ownership wired into execution and dashboard; assignment uses actual broker intervals and skips closed holidays/weekends; deterministic close/reopen and DST tests | Verify PnL ownership using real account updates across close/reopen and DST; code tests do not substitute for broker evidence |

All entries in `P0_GATES` remain `complete=False`. Approval requires reviewed
evidence for every row and a deliberate source change; it must not be inferred
from passing unit tests.

## P1 — Research integrity and deployment evidence

These tasks follow the P0 safety requirements in importance. Data repair and
local verification can proceed while broker-validation access is unavailable.
Existing implementations stay in the evidence section rather than being listed
again as unfinished features.

| ID | Item / status | Completion evidence |
|---|---|---|
| P1-02 | **Validate broker data ingestion and revision semantics — Implemented, validation pending.** Consolidates the historical-fetch, cross-asset freshness and adapter-shutdown follow-ups. | Supported TWS/Gateway evidence for historical fetch, replacement/merge, cadence, RTH/extended hours, permissions, pacing, shutdown, missing peers and revised/out-of-order bars. Verify that recovered or revised data cannot create duplicate decisions or lookahead. Link evidence to the affected P0 gates. |
| P1-03 | **Calibrate equity simulation — Implemented, calibration pending.** Finite books, partial fills, gap/session scenarios, fixed-plan costs, corporate actions and excess-return Sharpe already exist. | Dated spread/impact, participation, commission, regulatory, borrow/financing and issuer-action evidence, with sources and coverage by symbol/date. Compare predicted fills/costs with observed execution; retain both OHLC paths and stressed costs. Report uncovered periods as scenarios and record tolerances before evaluating agreement. |
| P1-04 | **Complete supported-version paper and fault-injection campaign — Validation pending.** Includes short-control recall proxies, account evidence, corporate-action recovery and all P0 rows. | Versioned TWS/Gateway, API and Nautilus compatibility matrix; reproducible fixtures, logs and reconciliation reports for each required scenario; supervised long-only baseline, then short-enabled tests and multi-week soak. Review inventory/fee/forced-cover proxies rather than claiming an unavailable native recall feed. Every P0 gate needs its own linked evidence and reviewer decision. |
| P1-05 | **Complete immutable research-to-promotion evidence — Validation pending.** Multi-seed comparison, robustness and one-shot holdout workflows are implemented. | One reproducible campaign with locked data/universe, seeds, folds, embargoes, assumptions and params; normal/stressed development results; independently reviewed finalists; single-use holdout; shadow/paper agreement. Preserve failed or interrupted holdout consumption. Use the sequential promotion procedure below; do not tune against inspected holdout results. |
| P1-06 | **Verify complete dashboard workflows — Proposed.** Browser verification was unavailable in the most recent equity-simulation session. | Browser integration coverage for data validation, backtest, optimization, campaigns, evidence upload/review and paper safety operations: configuration, launch, progress/status, cancellation and results. Exercise reload/reconnect, API failures, duplicate requests, invalid JSON, missing data and stale status. Prove no supported workflow is CLI-only and no unknown state appears safe. |
| P1-07 | **Establish an off-host recovery target — Open.** Local backup/restore, rollback and durable risk state already exist. | Encrypted off-host backups with retention and integrity checks; documented recovery-time/data-loss targets; a restore drill on a clean host that preserves kill-switch, allocation, audit and holdout-consumption state and reconciles with the broker before resuming. Dashboard configuration, backup status and restore evidence are required. |
| P1-08 | **Authorize remote operations before remote exposure — Open, conditional blocker.** Local authenticated controls exist. | Define identity, authorization by action, session expiry, transport protection and audit for remote use. Verify unauthorized and replayed requests fail; preserve action/target confirmation and fail-closed state. Keep the current local operating scope until validated. This is a blocker for remote deployment, not a requirement to expose the application remotely. |

## P2 — Operational and product improvements

| ID | Item / status | Completion evidence |
|---|---|---|
| P2-01 | **Add point-in-time dataset provenance and universe review — Proposed.** Extend existing CSV hashes and locked validation contracts. | Reviewable source/retrieval timestamps, volume and adjustment semantics, symbol/conId lineage, revisions and universe membership dates. Preserve delisted/renamed instruments where supported and explicitly disclose survivorship or coverage limitations. Surface the snapshot and any changes in the research hub. |
| P2-02 | **Automate drift and execution-quality review — Proposed.** Extend existing ML metrics, telemetry and alerts. | Compare research, shadow and paper predictions, fills, slippage, reject rates, feature freshness and regime behavior against declared baselines. Show sample size and uncertainty, configurable review thresholds and alert history. Model/parameter changes require a new reviewed version; alerts must not silently retune or promote models. |
| P2-03 | **Add machine-wide research admission and resource visibility — Proposed.** Current CPU/RAM budgets apply per process pool. | Queue concurrent jobs against one host budget while reserving capacity for TWS, risk supervision, Redis and local inference. Dashboard shows queued/running state, resource limits, cancellation and measured peak memory. Verify competing studies cannot starve execution supervision. |
| P2-04 | **Improve benchmark return-basis comparability — Proposed.** Same-period S&P 500 price-return benchmarking already exists. | Add an explicitly sourced S&P 500 total-return benchmark when available, retaining the price index as a labeled fallback. Show exact overlap, dividend treatment and comparable risk-free assumptions; never splice incompatible bases or conceal partial coverage. |
| P2-05 | **Add automatic corporate-action discovery and review — Proposed.** The current journal is operator-maintained. | Dated issuer/vendor evidence with deduplication and symbol/conId matching, an operator review queue and pending-event cancellation. Preserve flat-account recovery until an independently validated in-position policy exists; ambiguous events freeze affected execution rather than being auto-applied. |
| P2-06 | **Use an exchange calendar for synthetic equity fixtures — Open.** Current generation skips weekends only. | Fixtures respect holidays, early closes and DST, with explicit calendar/version metadata. Keep synthetic data visibly separate from market evidence and verify bar/session expectations in the dashboard. |
| P2-07 | **Support tiered commission and financing schedules — Open.** Fixed-plan per-order minimums/caps and configurable regulatory fees/financing already exist. | Dated selectable plans, account/currency and balance tiers, accrual/day-count rules and statement reconciliation. Lock the plan into every fold and expose assumptions and cashflow reconciliation in results. Prioritize only plans relevant to the intended account. |
| P2-08 | **Complete accessibility and resilience review — Proposed.** Apply the existing product/design contract. | Keyboard-only configuration and safety flows, focus recovery, programmatic labels, announced stale/error/progress states, chart text summaries and usable narrow layouts. Verify long evidence files, empty/partial results and network interruption without losing unsaved configuration. |
| P2-09 | **Measure target-host scaling and release reproducibility — Validation pending / proposed release check.** Local performance improvements already exist. | Run representative serial/parallel benchmarks on the actual target host; record hardware, dependency locks, worker count, peak memory and fold-result parity. Verify clean installation and dashboard/API startup. Do not infer throughput from advertised hardware or change folds/model settings to improve a benchmark. See `MAC_STUDIO_SCALING.md` for the measurement procedure. |
| P2-10 | **Keep operator documentation and readiness evidence synchronized — Proposed.** | Check launcher commands, supported workflows, parameter upload behavior, simulation assumptions and live-lock descriptions against the application. Link each completed backlog item to code/test or broker evidence with date and reviewer. Remove duplicate todo lists and stale “unimplemented” claims when closing work. |

## P3 — Optional research and scope extensions

These are lower priority than safety, usable data and a validated paper record.
They do not authorize changing the currently supported asset or execution scope.

| ID | Item / status | Completion evidence before adoption |
|---|---|---|
| P3-01 | **Evaluate conviction-based replacement of existing holdings — Proposed experiment.** New entry slots already rank by conviction. | Opt-in research comparison against the current no-eviction policy, with turnover, fees, hysteresis, risk rails and out-of-sample evidence. Require an explicit promotion decision before changing paper/live behavior. Consolidates the inline suggestion in the strategy documentation. |
| P3-02 | **Evaluate tick/event replay and queue-aware execution — Deferred.** OHLC paths cannot reconstruct true intrabar order or hidden liquidity. | Separate timestamp/data-quality and execution contracts, suitable observed data, measured fidelity benefit and bounded runtime/storage costs. Keep OHLC scenario limitations explicit until validated. |
| P3-03 | **Evaluate derivatives or mixed-asset portfolios — Deferred, outside current scope.** | Separate instrument/account/venue, fee, margin, lifecycle and portfolio-risk designs; a complete frontend workflow and independent research/paper validation. Current runs remain one asset class per account/venue contract. |

## Backlog maintenance and completion rule

P0 requirements live only in the gate register; P1–P3 rows refer to those gates
rather than creating separate versions of their requirements. The validation
campaign below is the ordered execution procedure, not a second backlog.
`AGENTS.md` and `CLAUDE.md` link here instead of maintaining residual task lists.
`PRODUCT.md`, `DESIGN.md`, `NEWS.md`, `OPERATIONS.md` and
`MAC_STUDIO_SCALING.md` remain supporting specifications, evidence and runbooks.

A user-facing capability is complete only when configuration, launch,
progress/status, cancellation where applicable and result review are available
in the frontend. Closing implementation work requires relevant verification;
closing a validation gate requires recorded external evidence where specified.
Record the completion date and evidence link here, then move the finished item
to implementation evidence. Local tests alone never approve live capital.

## Local implementation verification

**P0 Real-time risk — sector evidence and adapter disconnect implementation,
2026-09-09.** Reviewed, expiring conId/symbol-bound sector declarations now feed
gross stock/ETF exposure checks at pretrade, final submission and continuous
supervision. Broker holdings and outstanding entries are included; unknown
exposure fails closed. Execution-adapter callbacks invalidate a reconciliation
generation and freeze entries immediately. Reconnection requires fresh
reconciliation. Dashboard configuration, evidence validation, limit overrides,
session cancellation/restart, status and source review are implemented.

Completion review fixed nested optimizer parameters overriding the dashboard's
risk settings, below-market proposed-order valuation, malformed persisted upload
rendering and stale/future telemetry authority. Python regression coverage includes
sector valuation, expiry, identity, callback loss, reconciliation and supervisor
faults; frontend tests cover override precedence and freshness. The full suite
passed 452 Python tests; after adding a timestamp-parser regression, all 17
targeted supervisor/operations tests pass. All 20 frontend tests and the production
build pass. Browser verification could not run because no browser is available.
Supported TWS/Gateway disconnect, rejection, restart and sector-breach validation
remains pending. These implementation results do not approve the P0 gate.

**P0 Broker source of truth — execution identity hardening, 2026-09-09.**
Reconciliation now compares execution identity, side, quantity, price and
correction lineage with durable fills and other callbacks in the same snapshot.
Conflicting IDs produce critical `CONFLICTING_BROKER_EXECUTION` evidence and a
freeze/review action; ambiguous fills are never selected by callback order or
recovered. Identical snapshot duplicates recover once. Corrected executions can
be replayed after ledger restoration without replacing or double-counting fills.
The existing strategy audit, uncertain reconciliation telemetry and dashboard
safety controls carry the failure through to operator review and blocked resume.
Regression coverage: `tests/test_reconciliation.py` and
`tests/test_execution_state.py`, including strategy safety/audit integration.
Supported broker callback, permanent-ID and restart-race validation remains
pending; this change does not complete or approve the P0 gate.

**P1-01 — Equity volume repair and research preflight completed, 2026-09-08.**
[Recorded source, hashes and verification](evidence/P1-01-2026-09-08.json).
Equity historical fetches now default to LAST/TRADES; explicit MID remains a
price-only diagnostic source. The local QQQ CSV was refreshed through paper
Gateway port 4002 (server protocol 223): 2,934 observed bars from 2021-09-02 to
2026-09-08, all with positive traded-share volume. The original 2,934 zero-volume
bars are retained in a SHA-256-named backup. This refresh changes the observed
date range; it does not manufacture historical volume or rewrite locked research
snapshots. Prices are source-declared split-adjusted, not dividend-adjusted.

A read-only preflight reports per-ticker dates, requested width and observed
cadence, source/session/price/volume semantics, missing/invalid/zero volume,
duplicate timestamps, invalid OHLC and possible gaps. Unknown semantics and
exact missing-bar coverage remain explicit. API admission, both research CLIs,
and the shared equity engine block unusable execution data, including individual
optimizer folds; zero-volume bars still supply no liquidity. Diagnostic reports
remain available for rejected data. Reports are retained in simulation artifacts.

Backtest, optimization and campaign configuration expose preflight review and CSV
import. Standalone IBKR repair has configuration, durable job status, cancellation,
logs, original-data backup and result review. Fetch-enabled research validates
new data before simulation. Replacements with absent tickers, missing volume or
zero-only liquidity fail before publication. Adding missing peers preserves the
recorded source bar width across shortened RTH tails. Passing checks: 395 Python
tests plus 29 targeted checks after the source-cadence regression, 15 frontend tests, production build, a real Gateway data repair and a 240-bar
observed-data ingestion/artifact smoke (zero trades; not performance evidence).
Codex reviewed the implementation and resolved the frontend finish review's
workflow findings. Browser verification was unavailable and remains tracked in
P1-06; this completion does not approve any P0 or broker compatibility gate.


Corporate actions and instrument identity are implemented, validation pending.
Execution qualifies US-listed USD stock/ETF contracts, persists model history by
broker conId, and retains reviewed symbol aliases. The authenticated dashboard
provides split, cash-dividend and symbol-change event recording, status review
and pending-event cancellation. Recovery freezes entries, requires a flat
broker account after the effective instant, and rebuilds model and order state
from fresh broker history while preserving allocation and risk state. Interrupted
rebuilds repeat the flat-account and affected-instrument checks. Dividend cash
is taken only from broker account updates. This is an operator-maintained
journal, not automatic corporate-action discovery or an in-position adjustment
engine. Supported TWS/Gateway evidence for splits, reverse splits, dividends,
symbol/conId replacements and interrupted recovery is still required under the
broker-source-of-truth and order-lifecycle gates.

Equity research now uses finite synthetic L2 auctions before each completed
bar, with next-book execution, shared participation limits, partial fills,
explicit OHLC path selection and zero liquidity on zero-volume bars. Backtest
progress batches keep auctions with their completed bars, including multi-symbol
timestamps. The dashboard exposes execution/cost configuration, dated calibration
and corporate-action JSON attachment/review, launch, progress, cancellation and
result evidence through the existing research workflow. Saved artifacts retain
the assumption hash, coverage, commissions, financing cashflows and action audit;
partial calibration coverage is labeled explicitly. Optuna locks the same
assumptions into normal/stressed folds and the holdout contract.

Accounting includes marked unrealized PnL, long/short dividend receivables,
split rebasing and fractional-share cash-in-lieu PnL, order-level commission
minimums/caps, configurable regulatory fees, calendar-day borrow/debit/cash
accrual and interval-matched risk-free returns for Sharpe. Cash financing uses
purchase principal rather than unrealized price changes. Local tests cover
these contracts, API forwarding and streamed/one-shot replay parity. This is
an OHLC scenario engine: it does not establish historical fill accuracy or
certify supplied calibration evidence. Historical calibration and broker
validation remain outstanding; live-capital gates are unchanged.

Dashboard operator safety controls now provide authenticated local paper-session
freeze, guarded resume, cancel-all for flat books, flatten and permanent kill.
The API binds commands to registered jobs, verifies a server-configured token
and local origin, requires exact action/target confirmation, and uses durable
request IDs for safe retries. The interface includes status/history and
cancellation of unclaimed requests. Unknown or stale health blocks resume;
the strategy rechecks reconciliation, risk, account freshness, data/session
health, supervisor health, outstanding orders and broker protection when the
command is consumed. Resume requests expire after 30 seconds. Operator freeze
and permanent kill persist immediately by account/strategy, independently of
shutdown snapshots, and supervisor recovery cannot release an operator freeze.
Command/audit writes are atomic; acknowledgement is not reported as broker
completion. Local fault tests cover cancellation/fill races, kill/restart,
expired resume, failed audit writes, duplicate requests and authorization.
Supported TWS/Gateway paper validation remains required by the order-lifecycle,
kill-switch, broker-protection and recovery gates. Remote operations remain open.

Settled cash now comes directly from IBKR's `accountSummary` `SettledCash`
callback, separately from total cash, available funds and buying power. The
execution client keeps account/currency-specific, process-local evidence with
observation time; reconciliation consumes only current evidence and records its
provenance in the audit event. Disconnects, reconnect subscriptions and IBKR
connectivity errors invalidate it. The supported USD book is selected explicitly
for Nautilus multi-currency accounts. Paper/Live configuration shows the
read-only monitor's settled cash, and execution telemetry shows the running
node's independent reading. Missing, malformed, disconnected and older-than-240s
evidence remains unavailable, including when dashboard polling fails. Local
tests cover these cases; supported TWS/Gateway settlement and reconnect evidence
is still required under the broker-source-of-truth gate. This adds an account
observation, not a settlement-based buying-permission rule.

The canonical readiness register now includes the previously omitted equity
short-controls gate. It remains unapproved and blocks activation even if every
other gate and runtime broker check passes.

Execution session configuration is available under **Session policy** in the
Paper/Live forms, as `session_policy` in job requests, and as
`--session-policy-json` in the runner. See [OPERATIONS.md](OPERATIONS.md) for the
contract. Local tests cover validation, API forwarding, window boundaries,
holiday/weekend PnL ownership and the unchanged readiness lock. None of these
changes approves live capital or claims a completed TWS paper campaign.

## Required validation campaign

Automated/fault-injection suites must cover session/DST/holiday/early-close
boundaries; partial/rejected/canceled/stale orders; cancel/fill races;
stop/target/OCA behavior; disconnects and uncertain submission; restart with
working orders; duplicate/corrected callbacks; kill-switch flattening; corporate
actions; halts/gaps/missing bars; account types; and supported IB API,
TWS/Gateway and Nautilus adapter versions.

Promotion is sequential and evidence-based:

1. Deterministic backtest.
2. Walk-forward out-of-sample validation.
3. Shadow execution with order submission disabled.
4. Supervised long-only equity paper baseline, followed by short-enabled paper.
5. Multi-week unattended paper soak with incident review for both directions.
6. Small-capital live canary with strict exposure caps.
7. Formal sign-off before any capital or universe expansion.

The live readiness gate cannot be approved before every P0 row passes its paper
and compatibility evidence. P1 items that affect the proposed live account,
instrument universe, or operating model become promotion blockers for that
deployment even if they are not globally complete.
