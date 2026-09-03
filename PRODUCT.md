# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

The primary visual surface is a local React/Vite reporting and operations
dashboard backed by FastAPI. The CLI remains a first-class interface for
backtest, optimization, data fetch, and paper trading. Live-capital controls
exist but are fail-closed until the production-readiness program is approved.

## Users

Solo builder-trader: the same person develops the system and trades their own
capital with it (starting equity default $5,000). Not built for other users
or clients today.

## Product Purpose

A production-shaped, two-layer systematic trading system that separates an ML
alpha layer from a strategy/risk layer, so the same `MLStrategy` class runs
unchanged across backtest, paper, and live modes — only the engine/venue
wiring changes. Built on Nautilus Trader against Interactive Brokers (crypto
via the ZEROHASH/PAXOS venue, equities via SMART routing), with Optuna-driven
hyperparameter search. Success means a strategy that has been validated
through walk-forward backtesting and out-of-sample Optuna scoring to a point
the owner trusts enough to run with real capital.

## Positioning

Combines a short-horizon Huber-regression mean-reversion alpha with two
no-lookahead regime-conditioning features (a walk-forward Markov
transition-matrix score and a GaussianHMM latent state), cross-asset ARDL and
spread features between universe peers, and fractional-Kelly conviction
sizing bounded by fixed hard risk rails (1% target / 0.25% cap per trade,
book-level leverage = 1, daily loss limit, drawdown kill-switch). When
position-count caps bind, slots are allocated by conviction (highest
`|yhat|`) rather than ticker order. The same strategy class is asset-class
agnostic (crypto spot and equity today) and mode-agnostic (backtest / paper /
live) — a property a hand-rolled, single-purpose backtest script could not
truthfully claim.

## Operating Context

- Run locally by the owner through either the React dashboard or CLI.
- The dashboard reviews run artifacts, compares metrics and traces, launches
  backtest/Optuna/paper/live jobs, tails job output, and exposes risk controls.
- Data sources: synthetic sample bars (pipeline exercising) and real IBKR
  historical bars fetched via TWS/Gateway (ports 7497 paper TWS / 7496
  live TWS / 4002 or 4001 Gateway).
- Live/paper trading requires TWS or IB Gateway running locally; crypto
  market data requires the PAXOS subscription. IBKR spot crypto is live-only
  in this product because paper accounts do not support its execution.
- Standard workflow: backtest -> purged nested walk-forward Optuna search with
  an untouched outer holdout -> copy the profile-tagged params into execution config
  -> equity paper trade or crypto shadow/demo validation -> formal readiness
  review. The CLI, API, node builder, and dashboard currently refuse live
  capital regardless of confirmation phrase or port.

## Capabilities and Constraints

- Two asset classes today, one per run: crypto (`CurrencyPair`, Zero Hash
  trailing-volume fees, 24/7 calendar) and equity (`Equity`, per-share fees, weekday
  calendar), selected through a canonical operating profile in the dashboard
  or via `--asset-class`. Crypto optimization uses Sortino; equity optimization
  uses Sharpe. The switch also changes session, bar, routing, quantity, fee,
  universe, and regime-threshold defaults.
- The shared execution path is shaped for spot crypto and SMART-routed US
  equities/ETFs, but live-capital startup is disabled. Paper trading is
  equity-only; short execution is an explicit opt-in behind fail-closed IBKR
  borrow, fee, Rule-201, margin, what-if, and supervised-cover controls.
- The web dashboard exists under `web/`; no iOS or Android interface exists.
- Backtest and Optuna reporting use real run artifacts. Paper/live nodes write
  an atomic strategy telemetry snapshot after every completed bar; the
  dashboard polls it every three seconds for yhat, HMM/regime state, positions,
  and risk. A separate read-only IBKR client backfills and continuously updates
  the selected ticker's current OHLC bar. Searched symbols without model
  telemetry stay market-only rather than receiving fabricated overlays. When
  no node exists, the panel shows deterministic demonstration data with an
  explicit no-broker-connection label.
- Open track toward paper trading and then live trading with real capital is
  the owner's explicit direction, not yet executed.
- The production-readiness gate register and validation evidence are maintained
  in `PRODUCTION_READINESS.md`. Fill-based broker OCA protection and continuous
  supervision now have implementation candidates, but they remain unapproved
  until supported TWS/Gateway paper and fault-injection validation completes.

## Evidence on Hand

No live or paper trading track record exists yet — the system has not traded
real or paper capital. Backtest and Optuna walk-forward metrics
(`oos_r2`, `dir_acc`, `ic`) exist as run output, not as a recorded historical
track record. The owner intends to progress to paper trading and then live
trading; real results should be recorded here as they accumulate rather than
fabricated in the interim.

## Product Principles

1. One strategy class, three modes — backtest, paper, and live run identical
   strategy logic; only engine/venue wiring changes.
2. Alpha and execution stay decoupled — the ML layer never places orders; the
   strategy layer never computes the forecast.
3. Risk rails are fixed, not tuned — position sizing, leverage caps, and
   kill-switch thresholds are structural guardrails, not Optuna search
   parameters.
4. No lookahead, ever — every feature (regime, cross-asset, HMM) must be
   computable from data available at time t only.
5. Ship pre-production honestly — known simplifications and unverified
   integrations are tracked explicitly rather than hidden, since real capital
   is the eventual target.

## Accessibility & Inclusion

The dashboard targets WCAG 2.2 AA. All controls require programmatic labels,
visible keyboard focus, and keyboard operation; charts require text summaries;
touch contexts use 44px targets. Connection, stale-data, and kill-switch states
must be expressed in text and announced to assistive technology. Unknown broker
or API state must never be presented as safe, idle, empty, or clear.
