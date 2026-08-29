# Production Readiness Program

Live-capital deployment is fail-closed. `quant.run.readiness` is the canonical
gate, the CLI and TradingNode builder enforce it before adapter startup, the API
returns HTTP 503, and the dashboard keeps live controls locked. There is no
environment-variable bypass.

Paper and live use the same `build_node` and `MLStrategy` execution path. Mode
changes only the broker account/port and whether capital permission can pass the
readiness gate. IBKR paper execution is restricted to long-only US equities and
ETFs; spot-crypto paper execution is unavailable at IBKR.

## Status vocabulary

- **Implemented, validation pending**: a fail-closed implementation and local
  deterministic tests exist, but supported TWS/Gateway compatibility and paper
  behavior have not been proven.
- **Open**: material implementation work remains.
- **Promotion gate**: evidence must be recorded before the readiness flag can be
  reviewed; code presence alone is insufficient.

## P0 gate register

| Gate | Current status | Implemented evidence | Required before approval |
|---|---|---|---|
| Order lifecycle | Implemented, validation pending | Idempotent order/fill ledger; submitted, acknowledged, partial, filled, canceled, expired, rejected and denied states; execution IDs, corrections, actual average fills, DAY equity entries, stale-entry cancellation, rejection suspension and alerts | Normalize and verify IBKR correction/permanent-ID callbacks; paper-test cancel/fill races, partial fills, stale replacement and every rejection class |
| Safe shutdown | Implemented, validation pending | Entry freeze; cancel-confirm stage; framework flatten-confirm stage; no signal resolution or submission in `on_stop`; orphan/residual detection | TWS tests for cancel timeout, disconnect during shutdown, optional keep/flatten policy, and broker orders owned by other clients |
| Broker protection | Implemented, validation pending | Actual-fill-based stop-market/take-profit OCA pair; partial-fill resize; adjustment replacement; restart reconciliation; authoritative telemetry distinguishes acknowledged OCA orders from model references | Verify IBKR OCA tags, transmit semantics, modification ordering, RTH/outside-RTH support, gap behavior and restart adoption against supported adapter/TWS versions |
| Kill-switch | Implemented, validation pending | Immediate entry freeze; cancel-confirm before emergency exits; fill confirmation; unresolved state remains disabled; structured operator alerts | Fault-injection paper tests for cancellation rejection, exit rejection, disconnect and residual positions |
| Exchange sessions | Implemented, validation pending | IBKR TradingHours/LiquidHours/timeZoneId parser; holidays, early closes, DST, overnight windows, phases, explicit halts and stale-data states | Capture and validate real contract-details variants; model exchange-wide unexpected closures and auction/halts using supported IBKR callbacks |
| Session policies | Implemented, validation pending | RTH, extended and custom policies; open/close buffers; auction flags; no-entry-before-close; session-end entry cancellation; outside-RTH order validation | Expose and validate the full policy at CLI/API boundaries; paper-test daily after-close signals and each supported order type |
| Session risk accounting | Implemented, validation pending | Daily risk reset keyed to exchange sessions with configurable overnight assignment; canonical equity execution cadence is now daily unless explicitly set | Define and test extended-session PnL ownership with live account updates across close/reopen and DST |
| Real-time risk | Partially implemented | One-second supervisor plus quote/trade updates; broker equity, drawdown, leverage, daily loss, data age, account availability, gross/symbol/order/concentration limits and price collars | Wire authoritative sector classification/exposure; broker heartbeat and explicit disconnect callbacks; margin/settled-cash/pre-trade checks; prove no bar-cadence dependency |
| Broker source of truth | Open | Actual strategy fills/positions and working orders drive the local ledger and exposure commitments; startup adoption freezes on unknown claimed orders | Reconcile all account positions/orders/executions including manual TWS and other client IDs; uncertain-submission recovery; account currency, buying power, available funds and settled-cash verification |

All entries in `P0_GATES` remain `complete=False`. Approval requires reviewed
evidence for every row and a deliberate source change; it must not be inferred
from passing unit tests.

## P1 workstreams

The following remain open and must stay visible in planning and promotion
reviews:

1. Account/regulatory controls: cash vs margin, settled cash, buying power,
   maintenance margin, PDT and IBKR what-if margin/commission checks.
2. Short selling: real-time borrow availability, HTB fees, recalls, forced
   buy-ins and SSR. Execution remains long-only until complete.
3. Corporate actions and instrument identity: conId-first persistence,
   split/dividend/symbol-change processing, order/state rebuilds, and a strict
   US/USD stock-and-ETF universe contract.
4. Equity simulation and scoring: spreads, slippage, participation, partial
   fills, gaps, session liquidity, real fees/financing, corporate actions,
   excess-return Sharpe, benchmark alpha/beta, turnover and cost sensitivity.
5. Model/data integrity: completed/revised/out-of-order/stale bar controls,
   cross-asset staleness, immutable training metadata/model version, and
   fail-closed execution metadata matching.
6. Recovery/operations: deterministic all-client reconciliation, uncertain
   submission recovery, watchdog/heartbeats, external alerts, manual cancel-all
   and flatten, immutable audit records, authentication/authorization and log
   redaction.
7. Dashboard operations: authoritative account allocation, broker orders,
   fills, rejection reasons, OCA guarantee state, session/data health and
   reconciliation are displayed; authenticated manual controls and full
   uncertain-state interlocks remain open.

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
4. Supervised long-only equity paper trading.
5. Multi-week unattended paper soak with incident review.
6. Small-capital live canary with strict exposure caps.
7. Formal sign-off before any capital or universe expansion.

The live readiness gate cannot be approved before every P0 row passes its paper
and compatibility evidence. P1 items that affect the proposed live account,
instrument universe, or operating model become promotion blockers for that
deployment even if they are not globally complete.
