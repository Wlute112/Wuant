# Product & Production Readiness

This is the single prioritized backlog for the product. Last consolidated:
2026-09-07. It incorporates remaining work from `AGENTS.md`, `CLAUDE.md`,
this readiness register, and the hardware-validation follow-up in
`MAC_STUDIO_SCALING.md`. Product, design, news and operations documents retain
their specifications and procedures; future work is tracked here.

The 2026-10-20 institutional-control audit added confirmed gaps and linked
existing validation requirements without treating code presence as production
evidence. [Dated findings and ownership verification](evidence/institutional-gap-audit-2026-10-20.json)
record the inspected code and comparison scope; the engineering reference is
[FINRA's algorithmic-trading control guidance](https://www.finra.org/rules-guidance/notices/15-09).

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
| Execution ownership | Local implementation complete; broker validation pending (2026-10-20) | One kernel-held broker-writer slot across local accounts/profiles/client IDs, acquired before adapter/cache construction; no expiry/takeover; runtime mutation guards; ownership retained through verified disposal; dashboard review, conflict rejection and owner stop | [Local verification](evidence/institutional-gap-audit-2026-10-20.json). Validate stop/pause/crash/restart and broker reconciliation on supported TWS/Gateway. Covers the supported single-host/local-user deployment only; a future multi-host router needs broker-effective fencing before expansion. Do not run older unguarded binaries concurrently. |
| Account state isolation | **Open — confirmed persistence gap.** Existing trader/cache namespaces are profile/mode-specific, not account-specific; strategy snapshots lack an account identity | The new execution slot prevents concurrent writers to shared caches; it deliberately preserves legacy namespaces and risk state | Account-bound cache/order-ID namespaces and snapshot identity; verified migration preserving permanent kill, daily halt, allocation baseline, order/fill ledger and model context. Reject ambiguous/wrong-account state before broker startup. Never reset old risk state merely by renaming a namespace. Full frontend migration/review required before parallel account support. |
| Broker source of truth | Implemented, validation pending | Broker-neutral all-account position/order/execution/account reconciliation; deterministic stale lifecycle/fill recovery; post-Nautilus reconciliation cache normalization; manual/foreign exposure detection; direct IBKR buying-power, available-funds and excess-liquidity fields; distinct account/currency-specific SettledCash with freshness, reconnect invalidation, audit provenance and dashboard review; immutable reports; unresolved state freezes execution | Prove IBKR snapshot-end ordering, permanent-ID/correction handling, all-client visibility and restart races on supported TWS/Gateway; validate SettledCash currency, settlement transitions, callback cadence and reconnect invalidation on supported cash and margin accounts |
| Real-time risk | Implemented locally; independent emergency actuation and allocation-contract work open | One-second in-strategy checks plus an independently launched supervisor with a required heartbeat, durable freeze/flatten/kill commands, telemetry/data-age checks, alert delivery and bounded service watchdogs; broker equity, drawdown, leverage, daily loss, account availability, gross/symbol/order/concentration limits, price collars, direct IBKR margin/PDT state and order-specific what-if checks | Complete the confirmed actuation/allocation follow-ups below. Reviewed conId-bound stock/ETF sector exposure and explicit adapter disconnect callbacks are implemented; prove behavior against supported TWS/Gateway under sector breaches, disconnect and rejection faults |
| Kill-switch | Implemented locally; durable automatic-kill recovery gaps open | Immediate entry freeze; cancel-confirm before emergency exits; fill confirmation; unresolved state remains disabled; structured operator alerts; immediate operational persistence for operator kills | Complete automatic-kill/crash/universe/allocation recovery follow-ups below, plus fault-injection paper tests for cancellation rejection, exit rejection, disconnect and residual positions |
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

**Confirmed P0 follow-ups from the 2026-10-20 audit (part of the gates above):**

- **Kill-switch/state recovery:** automatic risk kills currently rely on the
  strategy shutdown snapshot, while immediate operational persistence is used
  for operator kills. `_restore_loaded_state()` can return for a different
  universe before restoring risk, or skip legacy risk without an allocation
  baseline. Persist automatic kills independently and immediately; preserve
  permanent disable and daily-halt state through crash, universe/configuration
  changes and allocation migration. Add fault tests for every path.
- **Independent emergency actuation:** `ops/supervisor.py` detects faults and
  queues commands for the same strategy process. It cannot itself prove broker
  cancellation/liquidation when that process/event loop is wedged. Define an
  independently authorized broker/emergency path, old-writer fencing, durable
  intent and actual broker confirmations; test it with a stopped/dead strategy.
  Existing broker OCA protection and a manual TWS exit remain relevant; queued
  commands are not evidence of an emergency exit.
- **Allocation contract:** CLI paper startup without `--cash` currently selects
  full broker NAV, whereas dashboard paper sends an explicit allocation. Make
  the selected allocation explicit and consistent across CLI/API/frontend,
  and bind persisted risk baselines to that choice. Test default and changed
  allocations without resetting a halt/kill or silently sizing against a much
  larger broker account. This is part of real-time risk and account-state gates.

## P1 — Research integrity and deployment evidence

These tasks follow the P0 safety requirements in importance. Data repair and
local verification can proceed while broker-validation access is unavailable.
Existing implementations stay in the evidence section rather than being listed
again as unfinished features.

| ID | Item / status | Completion evidence |
|---|---|---|
| P1-09 | **Correct paper-soak continuity and coverage accounting — Open, confirmed defect.** `ops/validation.py` counts elapsed first/last observation time as runtime and calculates health only after removing observations through the last failure. | Accumulate only bounded, sufficiently dense observation intervals; enforce timezone/monotonic ordering and expected trading-session coverage; report gaps, restarts and failures without resetting the denominator. Bind evidence to account/model/release identity. Sparse daily observations and long outages must not satisfy a multi-week healthy soak. Configuration and result review in the dashboard. |
| P1-02 | **Validate broker data ingestion and revision semantics — Implemented, validation pending.** Consolidates the historical-fetch, cross-asset freshness and adapter-shutdown follow-ups. | Supported TWS/Gateway evidence for historical fetch, replacement/merge, cadence, RTH/extended hours, permissions, pacing, shutdown, missing peers and revised/out-of-order bars. Verify that recovered or revised data cannot create duplicate decisions or lookahead. Link evidence to the affected P0 gates. |
| P1-03 | **Calibrate equity simulation — Implemented, calibration pending.** Finite books, partial fills, gap/session scenarios, fixed-plan costs, corporate actions and excess-return Sharpe already exist. | Dated spread/impact, participation, commission, regulatory, borrow/financing and issuer-action evidence, with sources and coverage by symbol/date. Compare predicted fills/costs with observed execution; retain both OHLC paths and stressed costs. Report uncovered periods as scenarios and record tolerances before evaluating agreement. |
| P1-04 | **Complete supported-version paper and fault-injection campaign — Validation pending.** Includes short-control recall proxies, account evidence, corporate-action recovery and all P0 rows. | Versioned TWS/Gateway, API and Nautilus compatibility matrix; reproducible fixtures, logs and reconciliation reports for each required scenario; supervised long-only baseline, then short-enabled tests and multi-week soak. Review inventory/fee/forced-cover proxies rather than claiming an unavailable native recall feed. Every P0 gate needs its own linked evidence and reviewer decision. |
| P1-05 | **Complete immutable research-to-promotion evidence — Validation pending.** Multi-seed comparison, robustness and one-shot holdout workflows are implemented. | One reproducible campaign with locked data/universe, seeds, folds, embargoes, assumptions and params; normal/stressed development results; independently reviewed finalists; single-use holdout; shadow/paper agreement. Preserve failed or interrupted holdout consumption. Use the sequential promotion procedure below; do not tune against inspected holdout results. |
| P1-07 | **Establish an off-host recovery target — Implemented, external validation pending (deferred by user 2026-09-13).** Encrypted SFTP recovery, retention, integrity checks, dashboard configuration/jobs/status/cancellation and isolated restore evidence implemented. | **Still required for full completion:** actual off-host destination provisioning and verification; reviewed clean-host restore preserving kill-switch, allocation, audit and holdout-consumption state; measured end-to-end recovery/data-loss targets and fresh broker reconciliation before resuming. See [local evidence](evidence/P1-07-2026-09-13.json) and [recovery runbook](OPERATIONS.md#encrypted-off-host-recovery-p1-07). |
| P1-08 | **Authenticate every execution mutation and authorize remote operations before exposure — Open.** Dedicated safety/recovery controls are authenticated, but generic paper launch/job cancellation do not share that authorization contract. | Apply action/target identity, allowed origins, replay protection and audit to all execution-changing routes; verify generic job APIs cannot bypass safety controls. Preserve usable frontend credential/session flows and require authorization for stop as well as start. Remote deployment additionally needs scoped identities/roles, expiry and protected transport; keep current local scope until independently validated. |
| P1-10 | **Bind research, shadow, paper and deployment to one immutable model/release identity — Open, confirmed gap.** A model registry exists, but paper observations/campaign IDs do not bind the exact params, data, account or runtime revision; registry evaluation trusts a caller-supplied `ready` report. | Verify report provenance and hashes, account/allocation, params/feature schema, data contract, risk/session policy, code and dependency identity end to end. Reject reports from other models and runtime code drift; enforce approved artifacts at deployment rather than an optional CLI choice. Expose register/evaluate/approve/reject/rollback and evidence in the frontend. Keep P0 readiness independent. |
| P1-11 | **Reconcile independently to broker statements — Open.** Runtime cache/ledger reconciliation exists; no independent statement/Flex cash-and-position accounting workflow was found. | Import dated IBKR statements/Flex evidence, deduplicate executions and corrections by account/currency/conId, and reconcile fills, fees, dividends, financing, cash, positions and PnL with tolerances. Track unresolved breaks and operator decisions, preserving originals. Provide frontend import, progress/cancel and break review; no automatic cash adjustments. |
| P1-12 | **Implement an order-disabled shadow comparator — Open.** Prediction evaluation and drift proxies exist, but no independently replayable frozen signal/order-intent shadow path was found. | One immutable model/data/cost contract produces timestamped causal signals and proposed orders without a broker execution client. Compare against paper decisions/fills by exact identity/time, report missing data and disagreements, and block model promotion on unexplained divergence. Frontend configure/start/status/stop/result review; verify no order submission is possible. |
| P1-13 | **Control selection bias across research campaigns and reused holdouts — Open.** Purged nested folds and per-study/campaign one-shot holdout consumption exist; consumption/search history is not globally linked across renamed campaigns or repeated hypotheses. | Retain an immutable experiment/hypothesis ledger, related dataset/time windows, all attempted studies/feature variants and previously inspected holdouts. Report effective sample size and autocorrelation-aware uncertainty, multiple-testing/selection-adjusted evidence and simple benchmark/ablation comparisons. A renamed campaign must not make an inspected holdout untouched. Show uncertainty and reuse warnings before launch/promotion; never claim statistical evidence from synthetic fixtures. |
| P1-14 | **Acquire verified point-in-time universe and source coverage — Open, evidence gap.** Local snapshots/provenance exist; vendor historical availability, delisted coverage and many universe membership dates remain unknown. | Dated licensed/vendor or issuer evidence for availability/revisions, historical constituents, delistings, symbol/conId changes and corporate actions; causal as-of joins and coverage by symbol/date. Evaluate a point-in-time investible universe instead of silently reusing today's survivors. Expose missing coverage and source declarations in preflight/results. Extends P2-01 evidence; does not undo its completed local archive work. |
| P1-15 | **Add portfolio factor, correlation and liquidity stress budgets — Open, confirmed scope gap.** Gross/symbol/sector limits exist; industry-correlation alpha features are not portfolio risk controls. | Account-wide beta/factor and correlated-exposure aggregation; gap/correlation-break and liquidity/exit-cost stress scenarios; configurable estimated-loss and participation/capacity budgets with freshness/unknown states. Apply identical research/paper controls and review forecast-versus-realized risk. Full dashboard configuration/status/evidence; avoid presenting unstable VaR estimates as guaranteed loss caps. |
| P1-16 | **Measure actual execution quality and strategy capacity — Open.** P2-02's fill comparison uses the last strategy close; equity simulation has finite participation scenarios but lacks observed arrival-quote attribution. | Capture timestamped decision/arrival NBBO, spread, order/fill/cancel events and quote provenance; calculate signed implementation shortfall, fill probability, adverse selection and missed-trade cost net of statement fees. Measure costs and net edge versus size/turnover under normal/stress liquidity. Lock calibrations before out-of-sample review and expose coverage/uncertainty in results. Links P1-03 calibration and P2-02 review. |
| P1-17 | **Enforce backend release/CI and independent change review — Open, confirmed workflow gap.** Dependency locks and local Python tests exist; repository CI currently runs only frontend workflows. | Run Python/unit/fault suites and frontend contracts in CI on pinned clean environments; retain versioned results/build identities and adapter compatibility evidence. Require a reviewed immutable release and meaningful rollback tests before execution changes. Approval must identify reviewer and exact source/artifact, and remain separate from P0 capital permission. Includes P2-09 clean-install/release checks without replacing target-host measurement. |
| P1-18 | **Cross-check live data and time authority independently — Open.** Existing freshness/revision gates largely consume the same IBKR source, and the macOS doctor treats clock sync as a manual warning. | Independent timestamped price/quote samples or reviewed alternate-source evidence, divergence thresholds, measured clock offset/uncertainty and as-of watermarks. Stale, future or inconsistent data must freeze affected entries rather than appear healthy because both model and monitor share an error. Configure/review disagreements and coverage in the frontend; test outages, DST, latency and revised observations. |

## P2 — Operational and product improvements

| ID | Item / status | Completion evidence |
|---|---|---|
| P2-02 | **Automate drift and execution-quality review — Local implementation, validation pending.** A saved research run can be selected as the paper/live baseline in the dashboard. The API reviews exact-overlap decisions, cross-period prediction magnitude and regime shares, recent fills/rejections, market/telemetry age every 30 seconds; configured thresholds and alert transitions persist per job. See `api/drift.py`, `web/src/components/live-panel/DriftReview.jsx`, and `tests/test_drift_review.py` (2026-10-01). | Validate against observed paper execution and an independent shadow feed when available. Current fill-price comparison uses the last recorded strategy close, not arrival NBBO or statement costs; feature-level freshness and shadow agreement remain unavailable. Display sample coverage and Wilson intervals for proportions; do not silently retune or promote models. |
| P2-04 | **Improve benchmark return-basis comparability — Proposed.** Same-period S&P 500 price-return benchmarking already exists. | Add an explicitly sourced S&P 500 total-return benchmark when available, retaining the price index as a labeled fallback. Show exact overlap, dividend treatment and comparable risk-free assumptions; never splice incompatible bases or conceal partial coverage. |
| P2-05 | **Add automatic corporate-action discovery and review — Proposed.** The current journal is operator-maintained. | Dated issuer/vendor evidence with deduplication and symbol/conId matching, an operator review queue and pending-event cancellation. Preserve flat-account recovery until an independently validated in-position policy exists; ambiguous events freeze affected execution rather than being auto-applied. |
| P2-07 | **Support tiered commission and financing schedules — Open.** Fixed-plan per-order minimums/caps and configurable regulatory fees/financing already exist. | Dated selectable plans, account/currency and balance tiers, accrual/day-count rules and statement reconciliation. Lock the plan into every fold and expose assumptions and cashflow reconciliation in results. Prioritize only plans relevant to the intended account. |
| P2-08 | **Complete accessibility and resilience review — Proposed.** Apply the existing product/design contract. | Keyboard-only configuration and safety flows, focus recovery, programmatic labels, announced stale/error/progress states, chart text summaries and usable narrow layouts. Verify long evidence files, empty/partial results and network interruption without losing unsaved configuration. |
| P2-09 | **Measure target-host scaling and release reproducibility — Validation pending / proposed release check.** Local performance improvements already exist. | Run representative serial/parallel benchmarks on the actual target host; record hardware, dependency locks, worker count, peak memory and fold-result parity. Verify clean installation and dashboard/API startup. Do not infer throughput from advertised hardware or change folds/model settings to improve a benchmark. See `MAC_STUDIO_SCALING.md` for the measurement procedure. |
| P2-10 | **Keep operator documentation and readiness evidence synchronized — Proposed.** | Check launcher commands, supported workflows, parameter upload behavior, simulation assumptions and live-lock descriptions against the application. Link each completed backlog item to code/test or broker evidence with date and reviewer. Remove duplicate todo lists and stale “unimplemented” claims when closing work. |
| P2-11 | **Specify broker traffic budgets and emergency priority — Proposed, confirm intended-account limits.** Nautilus already supplies per-process submit/modify throttles; an explicit aggregate IBKR data/order/cancel budget and emergency queue policy is not configured here. | Lock broker/version-appropriate request, cancel/replace and subscription limits; reserve emergency capacity and test burst/reconnect/backfill behavior without starving risk exits. Expose throttling, queue age and rejected/delayed actions; preserve idempotence and exact broker acknowledgement. |
| P2-12 | **Anchor operational evidence outside the writable host — Open, external provisioning required.** Operational audit rows are locally hash-chained and protected by SQLite triggers; model artifacts and encrypted recovery already exist. | Independently anchored signed/hash checkpoints or append-only retained copies, receipt/delivery alarms, documented retention and restoration/rollback detection. Verify alert receipt and incident acknowledgement outside the strategy host. Extend the existing alert/recovery workflow; do not treat local triggers as protection against whole-database replacement or claim the user-deferred off-host recovery drill is complete. |

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

**P2-03 — Shared host research admission completed, 2026-10-20.**
[Verification and limitations](evidence/P2-03-2026-10-20.json).
Dashboard research jobs and research CLIs share a local-user SQLite admission
queue across API instances and checkouts. FIFO admission reserves CPU worker
slots and estimated aggregate memory, preserving the host's service reserve;
seed/fold pools inherit their parent reservation. CLI admission covers dataset
preparation, search, holdout and reporting. Paper nodes and risk supervisors
bypass the research queue. Native numerical threads are limited to one per
worker; dashboard research runs at lower scheduler priority.

The Backtest/Optimization workbenches expose authenticated host-limit editing,
queue position, running reservations, queued cancellation, retained per-job
sampled peak process-tree RSS and explicit stale/error states. Limits cannot
remove service reserves or strand active/queued requests. A one-second memory
watchdog stops research on a reservation breach or measurement loss. Crash
cleanup retains occupied capacity until owned research processes have exited.

Local checks include real Redis/detached-process contention, restart and
cancellation, supervisor crash cleanup, memory/measurement fault injection,
and an independent heartbeat under competing CPU research. Desktop/mobile
browser workflows and the production build pass. Admission estimates and
sampled RSS are not OS hard CPU/RAM isolation; short memory peaks and unrelated
processes can exceed the reserved headroom. This local service-responsiveness
check does not replace supported-TWS/Gateway fault and soak evidence or approve
any P0 gate. Target-host scaling remains P2-09.

**P2-06 — XNYS synthetic fixture calendar completed, 2026-09-26.**
[Verification and limitations](evidence/P2-06-2026-09-26.json).
Equity fixture generation uses pinned `exchange_calendars` XNYS sessions and
session-close UTC timestamps, with session open/close, duration, early-close,
calendar name/version and explicit synthetic source/basis columns. Research data
preflight rechecks every fixture session against the installed calendar and
shows session coverage, early closes and DST-dependent UTC open times in the
dashboard. The research data disclosure generates job-scoped fixtures with
configuration, status, cancellation, result review and CSV selection. Crypto
fixtures remain 24/7. These bars are demonstration inputs, not market evidence
or a claim about unscheduled historical exchange closures.

**P2-01 — Dataset provenance and universe review completed, 2026-09-21.**
[Verification and limitations](evidence/P2-01-2026-09-21.json).
CSV import and IBKR publication retain exact content-addressed versions, sealed
manifests and a separate source-observation journal. Revision review includes
added/removed symbols and bars, changed values/semantics, supplier retrieval
metadata, symbol/conId declarations and dated universe membership evidence.
Earlier data remains retained when symbols disappear or change names.
Backtest and optimization read pinned snapshots; multi-seed campaigns retain one
input across seeds and retries. Manifest and CSV integrity are checked before
robustness and holdout consumption. Existing hash-only contracts remain explicitly
legacy evidence; no historical provenance is invented for them.

Configuration preflight, campaign review and saved-run Evidence panels expose
provenance, revisions, unknown coverage and downloadable evidence. Existing
research/repair jobs provide launch, status, logs and cancellation. The runbook
covers optional CSV declarations, archive recovery and storage. Local archives
establish observation history only: vendor historical availability, survivorship
and delisted coverage remain unknown unless supplied. Membership dates are
review evidence; research continues to use its selected fixed universe. No P0
gate or live-capital permission changes.

**P1-07 — Encrypted recovery workflow implemented, 2026-09-13; external
completion deferred.** The authenticated Paper/Live recovery disclosure provides
SFTP/password-file/Redis configuration, retention and RPO/RTO targets, opt-in
API-lifetime scheduling, initialization, backup/full integrity checks, durable
job status, idempotent retries, cancellation, exact snapshot selection and
isolated restore reports. Bundles preserve campaign holdout files, SQLite state,
audit data and full Redis RDBs. Restore verifies data without activating it or
releasing execution gates. [Verification and limitations](evidence/P1-07-2026-09-13.json).
The actual off-host destination and clean-host/broker drill are explicitly still
required; the user asked to skip that external portion for now. No P0 gate is
approved by this implementation.

**P1-06 — Dashboard browser workflows completed, 2026-09-11.**
[Recorded environment, coverage and verification](evidence/P1-06-2026-09-11.json).
The Playwright suite drives the real dashboard through isolated HTTP fixtures:
data preflight/import/repair; backtest and optimization launch, progress, reload,
cancellation and results; all campaign stages; evidence upload/review; paper
configuration and safety actions; uncertain retries, duplicate clicks, stale
health, API failure and narrow-screen keyboard/focus recovery. All 24 browser
tests, 20 frontend unit tests, 32 relevant API/reporting tests and the production
build pass locally in Chrome. CI configuration runs the suite with Chromium and
retains reports and failure traces; that Linux job has not been run locally.

Browser verification exposed and fixed missing campaign evidence and job review,
stale/consumed holdout actions, repeated launch submission, and paper resume with
a stale heartbeat. Campaigns now display locked contracts, consensus, robustness
and holdout evidence plus stage progress, logs and cancellation. Unknown evidence
is explicit and locks stage actions. The [browser runbook](web/e2e/README.md)
documents isolation and reproduction. These are local browser/API workflow tests;
P0 broker validation, historical calibration and paper soak evidence remain
pending. Broader accessibility review stays in P2-08.


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
