## Interaction Style
- Do not narrate your actions.
- Omit conversational filler (e.g., "Let me check that").
- Output only code changes, errors, and direct answers.

# Two-Layer Quant System — Nautilus Trader + Interactive Brokers (Crypto)

A production-shaped rebuild of the research notebook into two cleanly decoupled
layers, wired for **cryptocurrency** (spot BTC/ETH/SOL/... vs USD, IBKR Zero Hash
venue) via **Nautilus Trader**, with **Optuna** hyperparameter optimization.
Crypto is the focus because it trades **24/7** — no market-closed gaps, so the
synthetic bars and real IBKR history are continuous calendar-day series.

```
   ┌────────────────────┐      yhat       ┌────────────────────┐     order objects
   │  ML / ALPHA LAYER   │ ─────────────▶  │  STRATEGY LAYER     │ ──────────────────▶ IBKR
   │ prediction_engine   │  fwd-return     │ ml_strategy +       │  LIMIT (maker) /
   │ Huber regression    │  forecast       │ risk manager        │  MARKET (taker)
   │  + regime features  │                 │                     │  (fractional coins)
   └────────────────────┘                 └────────────────────┘
```

**Regime awareness.** The Huber alpha is a short-horizon mean-reversion model
and is structurally weak to regime changes. `models/regime.py` adds two
walk-forward, no-lookahead features it conditions on: a Markov transition-matrix
score (`P(next=Bull) - P(next=Bear)` from a 3x3 matrix over 20-day Bull/Bear/
Sideways states) and a GaussianHMM latent-state label (hmmlearn). Both slot into
the same feature vector the regression already used — see "Regime features".

The alpha layer never knows about orders; the strategy layer never knows how
`yhat` is produced. **The same `MLStrategy` class runs in backtest, paper, and
live** — only the engine/venue wiring changes.

---

## Layout

```
quant/
├── models/
│   ├── prediction_engine.py       # Huber-regression yhat, walk-forward, no-lookahead
│   └── regime.py                  # HMM + transition-matrix regime features (no-lookahead)
├── strategies/
│   ├── risk.py                    # 1% sizing, 0.25% cap, daily/kill-switch rules
│   └── ml_strategy.py             # the ONE Nautilus Strategy (backtest=paper=live)
├── data/
│   ├── generate_sample_bars.py    # synthetic bars so you can run tonight (no TWS)
│   └── ibkr_fetch.py              # real IBKR historical bars (needs TWS/Gateway)
├── run/
│   ├── backtest_common.py         # engine assembly + IBKR-Pro fee model
│   ├── run_backtest.py            # runnable backtest example
│   ├── run_live.py                # paper/live TradingNode (same strategy)
│   └── artifacts.py               # persists run results to quant/runs/*.json (dashboard reads these)
├── optimize/optimize.py           # purged nested walk-forward Optuna + outer holdout
├── api/                           # FastAPI backend for the reporting dashboard (Stage 6)
├── web/                           # React frontend for the reporting dashboard (Stage 6)
└── requirements.txt
```

---


```bash
pip install -r quant/requirements.txt
```
---

## Stage 1 — Backtest (works tonight, no TWS)

> The **backtest** already runs the ML alpha **and** the strategy *together*:
> on every bar `MLStrategy` asks `PredictionEngine` for `yhat` and turns it into
> orders. That is the command that places trades and reports PnL — by design,
> because the **strategy layer is the only thing that trades**.
> The separate `prediction_engine` command is an **alpha-only sanity check**: it
> prints walk-forward out-of-sample metrics and places **no** trades / no PnL.
> You do **not** need to "combine" them by hand — the backtest already does, and
> Stage 2 (Optuna) just re-runs that same combined backtest many times.

```bash


# 2. FULL BACKTEST = Huber-regression alpha (+ regime features) + strategy,
#    run together. MLStrategy calls predict_move() (yhat) on every bar and
#    submits orders. THIS is the command that produces trades and a PnL report.

python -m quant.run.run_backtest --csv quant/data/ibkr_bars.csv \
    --params quant/optimize/best_params.json \
    --tickers BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BCH
    --cash 5000

python -m quant.run.run_backtest --csv quant/data/equity_bars.csv --asset-class equity --tickers QQQ --cash 5000

# 3. ALPHA-LAYER SANITY CHECK ONLY — prints walk-forward OOS metrics
#    (oos_r2 / dir_acc / ic) plus the learned regime weights. No trades, no PnL.
python -m quant.models.prediction_engine --csv quant/data/ibkr_bars.csv --ticker BTC

# 4. REGIME FEATURES ONLY — emit the transition-matrix probabilities + HMM
#    latent states as a DataFrame aligned to the price series (optionally --out).
python -m quant.models.regime --csv quant/data/ibkr_bars.csv --ticker BTC

# 5. EQUITY/ETF EXAMPLE — same pipeline, --asset-class equity picks the right
#    instrument type (Equity, whole shares) and fee model (per-share commission
#    instead of crypto's tiered Zero Hash schedule) everywhere it matters. One
#    asset class per run (see "Asset classes" below) — omit --tickers to get
#    a default liquid
#    ETF universe (SPY QQQ DIA IWM).
python -m quant.data.generate_sample_bars --asset-class equity --out quant/data/equity_bars.csv
python -m quant.run.run_backtest --csv quant/data/equity_bars.csv --asset-class equity --tickers SPY QQQ --cash 5000
```

### Asset classes: crypto vs equity

Every command in Stages 1-3 that touches an instrument (`run_backtest`,
`optimize`, `generate_sample_bars`, `ibkr_fetch`) takes `--asset-class
{crypto,equity}` (default `crypto`, so every existing command above keeps
working unchanged). **One asset class per run** — a single run's tickers are
all built as the same instrument type on the same venue (Nautilus binds one
fee model per venue/account, and mixing crypto + equity in the same
book/account isn't supported). What actually changes per class:

| | crypto (default) | equity |
|---|---|---|
| Instrument | `CurrencyPair` (fractional size) | `Equity` (whole shares) |
| Fee model | tiered by trailing 30d volume (`backtest_common.ZeroHashCryptoFeeModel`) | flat per-share (`PerContractFeeModel`, `backtest_common.make_equity`) |
| Synthetic calendar | consecutive calendar days (24/7) | weekdays-only (`_business_days`, no holiday calendar) |
| IBKR fetch contract | `CRYPTO` / Zero Hash, `use_rth=False` | `STK` / `SMART`, `use_rth=True` |

The ML/risk layers (`models/prediction_engine.py`, `models/regime.py`,
`strategies/risk.py`, `strategies/ml_strategy.py`) need **no changes** for
either class — sizing already rounds via the active instrument's own
`make_qty()`, and the risk rules are pure dollar/%-of-equity math.

`run/run_live.py` also accepts `--asset-class {crypto,equity}`. Equity uses
SMART-routed `STK` contracts, regular trading hours, LAST bars, and the
instrument's whole-share quantity precision. `--primary-exchange` is optional
for disambiguation; without it, the adapter qualifies the SMART contract.


> **Alpha/backtest alignment.** The model the strategy trades with is refit each
> bar via `PredictionEngine.refit_on_history()`, which uses the *same* expanding,
> past-only windowing contract as the offline `walk_forward()` evaluation. So the
> model that generates the OOS metrics in command 3 and the model that places
> trades in command 2 are produced the same way — no truncated-window or
> stale-refit drift between them.

## Stage 2 — Optimize (Optuna)

```bash
python -m quant.optimize.optimize --csv quant/data/sample_bars.csv \
    --trials 40 --final-test-frac 0.20 --walk-forward-folds 5 --seed 42
```

```bash
python -m quant.optimize.optimize --csv quant/data/ibkr_bars.csv \
    --trials 40 --final-test-frac 0.20 --walk-forward-folds 5 --seed 42
```

**Two stopping modes.** By default (or with `--trials N`) the search runs a fixed
number of trials. Pass `--score FLOAT` for **goal mode**: Optuna keeps proposing
trials until a *completed* trial's stability-aware walk-forward value reaches the target, then
stops. `--trials` becomes an optional safety cap in goal mode (omit it to search
uncapped — Ctrl-C to abort). The run prints whether the target was `REACHED`.

```bash
# stop as soon as a trial scores >= 1.5 (safety cap of 200 trials)
python -m quant.optimize.optimize --csv quant/data/ibkr_bars.csv \
    --score 1.5 --trials 200 --seed 42
```

`optimize.py` also takes `--asset-class equity` (same meaning as Stage 1's
`run_backtest.py`), e.g. `python -m quant.optimize.optimize --csv
quant/data/equity_bars.csv --asset-class equity --tickers SPY QQQ --trials 40`.

Each trial re-runs the *same* combined
ML + strategy backtest from Stage 1 (the Huber model is still called on every
bar) and just searches the strategy hyperparameters
`n_lags, horizon, training_window_bars, entry_threshold, atr_period, atr_stop_mult, use_limit_orders,
limit_offset_bps, use_kelly_sizing, kelly_fraction, max_open_positions,
cross_asset_lags, spread_lags, huber_alpha, huber_epsilon`.

The newest 15–20% is an untouched outer test set. Earlier data is divided into
5–8 chronological folds; every fold freezes the Huber fit before an embargo of
`max(horizon, embargo_bars)` bars and enables trading only at validation start.
Every trial runs across the full ticker universe under normal and 2x commission/
slippage assumptions. The selection objective is
`median(fold_ratio) - 0.5*std(fold_ratio) - turnover_penalty - cost_sensitivity_penalty`,
and at least 60% of folds must be positive. Crypto uses net Sortino and equity
uses net Sharpe. `training_window_bars` compares expanding history (`0`) against
rolling windows derived from the configured warmup/minimum training size.
The locked winner is evaluated on the outer test once under each cost assumption;
the study is then immutable so later trials cannot tune against a seen holdout.
Saved output remains promotion-blocked until shadow and paper behavior agree.

**Fractional-Kelly conviction sizing.** When `use_kelly_sizing` is on, per-trade
size scales with the strength of the edge relative to its variance:
`f = kelly_fraction * |yhat| / var(fwd-return)`, capped at `kelly_max_fraction`
(50% of equity, a *fixed* rail). `kelly_fraction` — the "percent of full Kelly"
— is the knob Optuna tunes. Kelly only ever de-risks BELOW the flat sizer: the
result is floored at the same 1%-budget / 0.25%-cap / leverage=1 quantity, so
the hard risk rails stay binding (high conviction saturates at the cap, low
conviction sizes below it). `max_open_positions` caps concurrent holdings across
the crypto universe. When that cap binds, the slots are allocated **by
conviction**, not by ticker order: at each timestamp the strategy buffers every
instrument's signal and hands the free slots to the highest-`|yhat|` entries
(see `MLStrategy._resolve_batch`). Reversing/adjusting an already-held
instrument never consumes a new slot. (Slots free up when a risk event flattens
the book; there is no conviction-based *eviction* of an existing holding for a
later stronger signal — add that if you want slots to rebalance continuously.)

**Book-level leverage=1 guard.** `enforce_portfolio_leverage` (fixed, default on)
caps *aggregate* gross notional across all instruments at `max_leverage * equity`,
not just per-trade. Each new position is sized against the headroom left after
the notional already committed to other instruments (tracked synchronously at
order-submit time via `_committed_notional`, so resting-limit fill lag can't let
exposure slip past the cap). Without it, holding N coins each at the per-trade
notional ceiling could reach N× equity gross. At normal crypto volatility the
0.25% risk cap already keeps positions small, so this rail only binds in
tight-stop / low-vol regimes — it is insurance, not an everyday constraint.

Risk params (1% / 0.25% / leverage 1 / 2% daily / 5–10% kill, plus the fixed
`kelly_max_fraction` ceiling) and the regime features (`use_regime_features` /
`use_hmm_feature` / `regime_window`) are **fixed**, not tuned. Copy
`study.best_params` into `run_live.py` (`params=...`).

> Keep the LLM for higher-level reasoning (which features/regimes), not the float search.

## Stage 3 — Get real IBKR data (needs TWS/Gateway)

Note the socket port (paper TWS **7497**, live TWS **7496**; Gateway **4002/4001**).

Fetch:
   ```bash
   python -m quant.data.ibkr_fetch --tickers BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BCH \
       --years 5 --port 7497 --out quant/data/ibkr_bars.csv
   ```
   Crypto uses IBKR CRYPTO contracts on the ZEROHASH venue (base-asset code vs USD)
   with `use_rth=False` (24/7). If your adapter rejects `LAST` bars for crypto,
   switch the fetch to `1-DAY-MID` (MIDPOINT) — the backtest relabels bars
   regardless. Crypto market data on IBKR requires the PAXOS subscription.

   For equities/ETFs, add `--asset-class equity` — this switches to an IBKR
   `STK` contract, `SMART` routing, and `use_rth=True` (real market hours):
   ```bash
   python -m quant.data.ibkr_fetch --asset-class equity --tickers SPY QQQ \
       --years 5 --port 7497 --out quant/data/equity_bars.csv
   ```
   `--primary-exchange` (e.g. `ARCA`, `NASDAQ`) is optional, only needed if
   `SMART` routing can't disambiguate a ticker on its own. Like the crypto
   path above, this has NOT been exercised against a live TWS in this
   environment — verify against your installed adapter once TWS is up.
5. Re-run the backtest/optimizer on the real file (`--csv quant/data/ibkr_bars.csv`
   or `--csv quant/data/equity_bars.csv --asset-class equity`). Nothing else changes.

## Stage 4 — Paper trading (same strategy class)

```bash
brew services start redis
redis-cli ping
export TWS_ACCOUNT=DU1234567
python -m quant.run.run_live --tickers BTC ETH SOL --port 7497 \
    --params quant/optimize/best_params.json

python -m quant.run.run_live --asset-class equity --tickers SPY QQQ \
    --port 7497
```

The paper/live runner uses Redis-backed Nautilus cache persistence by default,
reconciles broker orders/positions at startup, restores model warmup and risk
state (including the permanent kill-switch), and requests historical bars on a
first run without allowing those historical bars to trade. See “IBKR
paper-trading setup” below. Use paper TWS port 7497 or paper Gateway port 4002.

The dashboard Paper/Live forms expose the same asset-class selection, optional
equity primary exchange, and short-selling opt-in. Strategy parameters are
selected with the browser's native JSON file picker; the frontend validates
the JSON and sends its contents to the API, which writes a job-scoped params
file for `run_live.py`. No server-side file path needs to be typed. The API
also forwards `asset_class`, `primary_exchange`, and `allow_shorts`.

## Stage 5 — Live trading (real money, explicit opt-in)

Only after paper results match expectations:
```bash
python -m quant.run.run_live --tickers BTC ETH SOL --live --port 7496
```
The runner refuses `--live` on the paper port as a guardrail.

---

## Stage 6 — Reporting dashboard (backtest results, live positions, risk, loss graphs)

A web dashboard ("Strip Recorder" — see `DESIGN.md`) for reviewing backtest/
Optuna runs, comparing them, watching equity/drawdown/ML-performance/regime as
continuous traces against the real fixed risk-rail thresholds, and triggering
backtest/Optuna/paper/live jobs from the browser instead of the CLI. Two new
pieces, both additive — nothing above this line changed behavior:

- `run/artifacts.py`: `run_backtest.py`/`optimize.py` now also write a JSON
  run artifact to `quant/runs/<run_id>.json` (equity curve, positions, fills,
  `StrategyMetrics`, per-ticker walk-forward ML performance, per-ticker
  regime series). Best-effort — a failure here never blocks the CLI's own
  printed results.
- `api/`: a FastAPI backend (`quant/api/main.py`) that serves those run
  artifacts over HTTP and spawns backtest/optimize/paper/live as subprocess
  jobs with status polling, log tailing, and cancel. `/api/jobs/live`
  requires an exact confirmation phrase AND a non-paper port before it will
  touch `run_live.py --live` — real capital stays behind that double guard
  even though the button exists in the UI.
- `web/`: a React + Vite frontend consuming that API.

**Dashboard-controllable settings** (all optional overrides; omitting them keeps
every existing default): Optuna sweeps can run a fixed trial count OR "goal
mode" (run until a target Sortino-like score is reached, `--score`, already
existed in `optimize.py` — the dashboard just exposes it); which alpha feature
blocks are on (AR/`n_lags`, regime transition-matrix, HMM, cross-asset
ARDL+spread) and whether the regime/HMM features are **fit** (jointly weighted
inside the Huber regression, original behavior) or **raw** (bypass the Huber
fit entirely and contribute `value * <feature>_raw_scale` directly to yhat —
the Huber model then fits only the residual after subtracting every raw
contribution; see `PredictionConfig.regime_source`/`hmm_source` in
`models/prediction_engine.py`); and the risk rails themselves
(`risk_budget_pct`, `max_trade_risk_pct`, `max_leverage`,
`daily_loss_limit_pct`, `kill_switch_pct`, `kill_warn_pct`,
`kelly_max_fraction` — see `strategies/risk.py`'s `RiskConfig`, now threaded
through `MLStrategyConfig` instead of being hardcoded at construction).
`run_backtest.py` accepts these via its existing `--params` JSON mechanism
(now also a STRUCTURAL_KEYS carrier, not just Optuna-tuned keys);
`optimize.py` accepts them via a new `--structural-json <path>` file, applied
identically to every trial and the final OOS validation (never searched by
Optuna). The dashboard's "actual vs predicted price" overlay and Sortino
ratio in the metrics panel both read from the same run artifact, reconstructed
from the same walk-forward predictions used for the ML-performance panel.

**IBKR data refresh modes**: both `run_backtest.py` and `optimize.py` accept
`--fetch-missing` to fetch only absent tickers and MERGE them into the CSV.
This mode detects the CSV's existing bar frequency and uses it; it never
accepts a different frequency, so missing-ticker data cannot be mixed with
old bars at another cadence. To intentionally change frequency, use
`--replace-bars --ibkr-bar-hours N`. That mode fetches the requested universe
and atomically REPLACES the CSV completely at the selected cadence. The
standalone `data.ibkr_fetch` CLI is also a complete replacement operation.
Both modes use the same IBKR connectivity requirements/caveats. The dashboard
exposes them as separate choices: "Add missing tickers · keep frequency" and
"Replace all bars · change frequency". The engine's own `BarType` label and
day-denominated regime/risk windows are not rescaled to match a sub-daily
fetch.

When `optimize.py` runs with `--replace-bars`, its `--ibkr-bar-hours` value is
recorded as `ibkr_bar_hours` in the saved params and run artifact. `run_live.py`
reads that value from the params JSON and uses it to choose the live bar width.

Run both from the SAME working directory every other command above uses
(the directory containing `quant/`, not `quant/` itself):
```bash
# backend (terminal 1)
quant/.quant312/bin/python -m uvicorn quant.api.main:app --port 8000

# frontend (terminal 2; static production build)
cd quant/web && npm ci && npm run build
../.quant312/bin/python -m http.server 5173 --bind 127.0.0.1 --directory dist
```
Live/paper positions and risk panels currently serve realistic MOCK data
(`api/live_mock.py`), clearly labeled "SIMULATED FEED" — Stage 4/5 paper/live
trading doesn't yet report real position state anywhere the dashboard can
read (see "Known simplifications" below), so those panels are wired for a
real feed but show synthetic values until that exists. Backtest/Optuna
reporting panels use real data from the run artifacts.

---

## Risk rules (implemented in `strategies/risk.py`)

| Rule | Value | Behavior |
|------|-------|----------|
| Risk budget / trade | **1%** of equity (target) | sizing target from ATR stop distance |
| Hard cap / trade | **0.25%** of equity | clamps size down if 1% would exceed it |
| Leverage (per trade) | **1** | single-position notional ≤ equity |
| Leverage (book) | **1** | `enforce_portfolio_leverage` (default on): summed gross notional across all instruments ≤ equity |
| Daily loss limit | **2%** of day-start equity | flatten all + 24h halt |
| Drawdown warn | **5%** peak-to-trough | logged warning |
| Kill-switch | **10%** peak-to-trough | **permanent** disable of automated execution |
| Kelly ceiling | **50%** of equity notional | `kelly_max_fraction` fixed rail; Kelly-sized qty then floored by the 0.25% cap |
| Capital base | **$5,000** | configurable (`--cash` / `starting_equity`) |

The 1% target and 0.25% cap are two distinct knobs: 0.25% is the binding ceiling
today; raise `risk_budget_pct` later as you scale.

Every value in this table is now **editable per run** from the CLI (`--params`
STRUCTURAL_KEYS on `run_backtest.py`, `--structural-json` on `optimize.py`) or
the dashboard (Stage 6) — `RiskConfig` is threaded through `MLStrategyConfig`
instead of being hardcoded at construction. (`max_leverage` and
`kill_switch_pct` were previously coded as 2.0/0.20 despite this table and
`risk.py`'s own comments already claiming 1/10% — fixed to match.)

---

## Regime features (models/regime.py)

Two extra columns are appended to the Huber feature vector, after the lagged
returns, so the alpha can condition on the prevailing regime:

| Feature | How it's built | Spec item |
|---------|----------------|-----------|
| `regime_score` | Label each bar Bull/Bear/Sideways from its trailing **20-day** return; build a **3x3 transition-probability matrix** from the state history seen *so far*; expose `P(next=Bull\|today) - P(next=Bear\|today)`. Recomputed **walk-forward** every bar. | Data preprocessing, Matrix construction, Feature engineering |
| `hmm_signed` | A **GaussianHMM** (hmmlearn) fit walk-forward on returns decodes the current latent state, ordered by mean return → {-1, 0, +1}. | HMM implementation (secondary feature) |

`build_regime_frame()` / `python -m quant.models.regime` emit BOTH as columns of
a DataFrame aligned to the price series (transition probs `p_bull/p_bear/p_side`
+ `regime_score`, and `hmm_state/hmm_label/hmm_signed`) — the requested output.

**No-lookahead:** every value at index `t` uses only `close[0..t]`, so slicing
into `walk_forward()` folds never leaks the future. Toggle via
`MLStrategyConfig.use_regime_features` / `use_hmm_feature` (structural, not
Optuna-tuned). The `RegimeFeatureEngine` is incremental/cached so the per-bar
refit in the backtest only computes the new tail (the HMM refits at most once
per `hmm_refit_every` bars, not every bar).

## Cross-asset features (models/prediction_engine.py)

Beyond its own AR lags, each instrument's Huber model can condition on OTHER
instruments in the run's universe via two more column blocks, appended after
the regime columns (`make_cross_asset_features`):

| Feature | How it's built |
|---------|----------------|
| ARDL lags | For each peer symbol, `peer_log_return[i-1] .. [i-cross_asset_lags]` — the peer's own lagged log returns (an ARDL term alongside the target's own AR lags). |
| Spread lags | `(own_log_return - peer_log_return)[i-1] .. [i-spread_lags]` — a stationary, scale-free basis series per peer, so mean-reverting divergence between two correlated assets is a direct regression input. |

Both blocks use ONLY log returns (scale-free, time-additive), so they compose
cleanly with the own-AR lags regardless of the two assets' price levels.
`cross_asset_lags` / `spread_lags` are Optuna-tuned (search space `0-5`, `0`
disables the block — see `optimize.py`); `peer_symbols`
(`MLStrategyConfig.cross_asset_symbols`) is structural, defaulting to every
OTHER ticker in the run's `--tickers` list.

**No-lookahead:** every ARDL/spread column is lagged `>= 1` (never the current
bar), which is deliberately more conservative than the own-AR lags need to be —
it means a peer instrument's bar for "today" doesn't need to have arrived yet
for the row to be computable, which matters because bars for different
instruments in the universe are not guaranteed to land in lockstep. A peer with
a shorter history than the target (e.g. a coin added to the universe later) is
right-aligned: its returns populate the most recent rows, and earlier rows get
a neutral `0.0` ("no signal yet") rather than raising. `MLStrategy` stays a
pure consumer — it only gathers `{peer_symbol: close_history}` dicts and hands
them to `PredictionEngine`; all feature construction lives in
`prediction_engine.py`. See `tests/test_cross_asset_features.py` for the
alignment/no-lookahead verification.

## Known simplifications / TODO before real money

- **Fee model:** crypto now models IBKR's real Zero Hash/Paxos schedule exactly
  (`ZeroHashCryptoFeeModel` in `backtest_common.py`, wired into
  `asset_class_fee_model` so both `run_backtest.py` and every `optimize.py`
  Optuna trial pick it up automatically via the shared `build_engine()`):
  tiered by trailing 30-day account-wide crypto trade value — 0.18% up to
  $100k, 0.15% $100k-$1M, 0.12% above $1M — with a $1.75 minimum per order
  that is itself capped at 1% of that order's trade value. Equities
  (`--asset-class equity`) remain a flat ~$0.005/share commission
  (`PerContractFeeModel`), with IBKR's real per-order minimum/tiered schedule
  not modelled for that asset class.
- **Bars:** daily OHLC only. Intraday/tick needs a different `BarSpecification`
  and IBKR market-data subscriptions.
- **Protective stops:** ATR stop distance currently controls position sizing,
  but the strategy does not submit a broker-side stop order. Daily-loss and
  drawdown controls remain active, but this must be implemented and exercised
  in paper before promoting the system to live capital.
- **Instrument:** `make_crypto` defines **spot** crypto (`CurrencyPair`, BASE/USD,
  fractional size). For crypto **perps/futures** add `CryptoPerpetual` /
  `CryptoFuture` definitions with the correct multiplier + funding.
- **Regime thresholds:** the 20-day Bull/Bear band (±2%), HMM refit cadence,
  and the regime-feature smoothing/hysteresis defaults in `models/regime.py`
  are all fixed, tuned implicitly for crypto's higher volatility — they are
  **not** auto-adjusted per `--asset-class`. Running `--asset-class equity`
  with these defaults will likely read mostly Sideways (index ETFs rarely
  move ±2% over 20 days outside a real crisis); override
  `regime_bull_threshold`/`regime_bear_threshold` in `PredictionConfig`
  directly if you want equity-scaled regime labels.
- **Equity trading calendar:** `generate_sample_bars.py --asset-class equity`
  skips weekends (`_business_days`) but does not model a real exchange holiday
  calendar — a documented simplification, same spirit as the crypto generator
  being "a pipeline exerciser, not market reality."
- **Equity live/paper scope:** `run_live.py` now supports SMART-routed US
  stocks/ETFs with whole-share sizing, RTH-only data, and LAST daily bars.
  Shorting is an explicit `--allow-shorts` opt-in and remains subject to IBKR
  permissions, borrow availability, and margin rules. Options, futures,
  extended-hours equity trading, and mixed crypto/equity runs are not covered.
- **Real API status:** `run_live.py` has been exercised end-to-end against
  paper TWS on port 7497: managed-account discovery, Zero Hash BTC/ETH/SOL
  contract qualification, account-state loading, execution reconciliation,
  one-year MIDPOINT history, continuing daily subscriptions, Redis save, and
  Redis restore all succeeded. The equity path was also verified with a
  SMART-routed SPY contract (qualified to ARCA), whole-share instrument
  precision, RTH LAST history (251 bars), continuing subscription,
  reconciliation, Redis save, and Redis restore. `ibkr_fetch.py`'s standalone
  CLI remains unverified against TWS.
- **Adapter shutdown noise:** a graceful paper stop saves state and exits zero,
  but Nautilus 1.229 may log IBKR error 162 for the intentionally cancelled
  historical subscription and a pending `_stop_async` task warning while its
  event loop closes. This is adapter cleanup noise, but should be rechecked
  after adapter upgrades.
- **State persistence:** paper/live now uses a Redis-backed Nautilus cache and
  persists strategy warmup/risk state. A clean restart recovers the permanent
  kill-switch and reconciles open positions/orders against IBKR. Redis is an
  operational dependency; monitor and back up its append-only volume before
  live deployment.
- **Dashboard live/paper data is mocked:** `api/live_mock.py` serves
  realistic sample positions/risk state, not real ones — nothing in
  `run_live.py` reports live position/risk state anywhere the dashboard (or
  anything else) can read yet. `/api/jobs/paper` and `/api/jobs/live` DO
  actually spawn the verified `run_live.py` paper/live path, but the resulting
  positions won't show up in the Live Positions panel until that reporting
  path is built.
```

# IBKR paper-trading setup

The paper runner trades spot crypto or equities through the same `MLStrategy`
used by the backtest. It persists orders, positions, account events, model
warmup history, daily halts, and the permanent kill-switch in Redis. Crypto
and equity use separate Nautilus trader/cache namespaces. Shorting is disabled
by default; equity runs may opt in with `--allow-shorts`.

## 1. Start persistence

From the directory containing `quant/`:

```bash
brew install redis
brew services start redis
redis-cli ping
```

Expected response: `PONG`. The Homebrew service binds Redis to localhost,
restarts it at login, and stores its database under
`/opt/homebrew/var/db/redis`. Enable append-only durability once:

```bash
redis-cli CONFIG SET appendonly yes
redis-cli CONFIG REWRITE
```

## 2. Configure TWS or IB Gateway

- Log in to the **paper** environment and verify that TWS visibly says paper.
- Enable API socket clients.
- Use port `7497` for paper TWS or `4002` for paper Gateway.
- Disable read-only API mode so orders can be submitted.
- Record the paper account id shown in TWS (commonly `DU...`).
- Ensure crypto trading permission and market data are available.

IBKR paper fills are simulated and can differ from live fills.

## 3. Start the runner

From the directory containing `quant/`:

```bash
export TWS_ACCOUNT=DU1234567
quant/.quant312/bin/python -m quant.run.run_live \
  --tickers BTC ETH SOL \
  --host 127.0.0.1 \
  --port 7497 \
  --client-id 1 \
  --cash 5000 \
  --params quant/optimize/best_params.json
```

On first startup, the strategy requests historical bars at the active bar
width to warm the model. Historical bars update model history but cannot
place orders. Only new, completed streaming bars are evaluated for trading.

Paper/live uses `MIDPOINT` bars for crypto because IBKR rejects Zero Hash
`LAST`/`AGGTRADES` bars with `keepUpToDate=True` (API error 321) — this holds
regardless of bar width. One-shot `LAST` history works, but cannot provide the
continuing stream required by the strategy.

By default paper/live subscribes to 1-day bars (`run_live.py`'s
`bar_type_suffix_for_asset`). Pass `--bar-hours {1,2,3,4,8}` to subscribe at a
sub-daily width instead — the only sizes IBKR supports for a continuing
EXTERNAL live subscription (same set `data/ibkr_fetch.py` treats as natively
fetchable). If `--bar-hours` is omitted, an `ibkr_bar_hours` value carried in
the `--params` JSON (e.g. an Optuna run that used `--fetch-missing` — see
above) is used instead, so loading that run's saved params for paper/live
automatically pulls data at the same cadence its training data was fetched
at. Explicit `--bar-hours` always overrides the params file's value.

The Zero Hash compatibility shim also waits for IBKR's `managedAccounts`
callback before execution-account validation. Without that wait, a warm Redis
instrument cache can make startup fast enough to race the callback and
incorrectly report an empty account set.

Stop with `Ctrl-C`. A clean shutdown saves strategy state. On restart, Nautilus
loads the Redis cache, reconciles open orders and positions against IBKR, then
restores the strategy's risk state and bar history.

Dashboard cancellation sends `SIGINT` to paper/live nodes and allows 30 seconds
for Nautilus to save state and disconnect cleanly before force-killing.

The CLI refuses known live ports (`7496` and `4001`) unless `--live` is passed.
It also refuses known paper ports when `--live` is passed.

## Diagnostics

To test connectivity without Redis, add `--no-persistence`. Do not use that
mode for an unattended paper session because the kill-switch and warmup history
will not survive a restart.

Logs should show all of the following before the session is considered ready:

- the requested IBKR account was found;
- execution reconciliation completed;
- each requested `BASE/USD.ZEROHASH` instrument loaded;
- daily-bar subscriptions started;
- Redis cache backing is enabled.

This runner remains crypto-only. Equity paper/live routing is not implemented.

Paper/live spot crypto is long-only: bearish signals flatten an existing long
and never open a naked short. Backtests retain their configured long/short
behavior.

`--cash` is the strategy allocation, not merely a fallback when the broker
account is available. Paper/live risk equity starts at that allocation and
then tracks the IBKR account's PnL delta from a persisted baseline. This keeps a
$5,000 test allocation from sizing against IBKR's much larger default paper
balance while preserving drawdown and daily-loss detection across restarts.
Legacy snapshots without an allocation baseline retain model history but
discard the incompatible full-account risk peak to avoid a false kill-switch.

`run_live.py --params` accepts both a flat strategy-parameter JSON object and
the optimizer's saved `best_params.json` format. For optimizer output it reads
tuned values from the nested `params` object, carries recognized structural
settings from the top level, and ignores run metadata such as trial count and
source CSV.
The Paper and Live dashboard forms expose the same optional params path and
pass it through the API to `run_live.py`.
