# Live news factor

`quant.news` converts IBKR headlines and a controlled RSS/Atom catalog into one
bounded, causal feature consumed by the existing `PredictionEngine`. It does
not place orders and does not bypass the strategy or risk layers.

## Runtime flow

1. `RssPoller` polls first-party and broad-market feeds with conditional HTTP
   requests, bounded concurrency, per-feed failure isolation, and deduplication.
2. `IbkrNewsClient` uses a dedicated read-only TWS client ID. It requests every
   provider returned by `reqNewsProviders()`, subscribes to each available
   BroadTape, subscribes generic tick `292` for every strategy contract, and
   asynchronously retrieves article bodies when the provider permits it.
3. `NewsProcessor` persists a deterministic bounded score immediately. A local
   Ollama model can replace it asynchronously; model failure leaves the
   deterministic score active.
4. `NewsFeatureReader` combines direct-symbol, broker-derived industry,
   commodity, and macro effects with source/confidence/urgency weights and
   exponential time decay. Its output is clipped to `[-1, 1]`.
5. In the default `raw` mode, the score contributes at most
   `news_raw_scale=0.001` to the predicted forward return. `fit` mode instead
   adds the score as a Huber feature.

The IBKR API cannot purchase news subscriptions. The client requests all
providers already available to the account, including free/default providers
when IBKR returns them. Missing permissions, unsupported provider BroadTapes,
article-body failures, and a disconnected TWS are isolated; RSS ingestion and
the trading risk controls continue.

## Paper/live

Paper/live starts the news service automatically:

```bash
export TWS_ACCOUNT=DU1234567
quant/.quant312/bin/python -m quant.run.run_live \
  --asset-class equity --tickers SPY QQQ XLE XLK \
  --port 7497
```

Defaults:

- database: `quant/data/news.sqlite3`
- TWS news client ID: `30` (must differ from the TradingNode client ID)
- RSS interval: 120 seconds
- local model: Ollama `lfm2:24b`
- IBKR providers: all codes returned for the account
- IBKR streams: provider BroadTape plus contract-specific tick `292`

The dashboard API uses the same default path. Set `QUANT_NEWS_DB_PATH` before
starting the API only when the dashboard should read a different standalone
collector archive; an active dashboard job's trusted telemetry takes priority.

Useful controls:

```bash
# RSS only
python -m quant.run.run_live ... --no-ibkr-news

# IBKR only
python -m quant.run.run_live ... --no-rss-news

# Restrict IBKR to provider codes returned by reqNewsProviders()
python -m quant.run.run_live ... --news-provider BRFG --news-provider DJNL

# Disable the factor and both collectors
python -m quant.run.run_live ... --no-news
```

The collector can also run independently of a trading node:

```bash
python -m quant.news.service \
  --asset-class equity --tickers SPY QQQ XLE XLK \
  --port 7497 --client-id 30
```

Do not run the standalone collector and the live runner with the same TWS
client ID at the same time.

## Dashboard rolling tape

The Paper and Live dashboard views include a collapsible feed docked to the
bottom-right. Narrow screens start with a collapsed launcher which opens a
bounded bottom sheet. It polls
`GET /api/live/news` every two seconds and shows the latest normalized RSS,
IBKR BroadTape, and IBKR contract-specific headlines from the shared database.
For a dashboard-managed node, the route resolves the database recorded in that
job's trusted telemetry, so a non-default `--news-db` does not silently fall
back to the canonical database.

Each row identifies whether its current analysis came from the local Ollama
model or the deterministic fallback. Connected ticker buttons show direction
and the article's news-only move estimate; selecting one loads that ticker in
the live chart. Expanding a row exposes the summary, confidence, urgency,
macro score, industry/commodity links, scope, and source article.

In the default raw-news mode, an enabled factor displays each article's causal
marginal share of the aggregate news feature after cutoff, age, decay, source,
confidence, urgency, and corroboration weighting, multiplied by
`news_raw_scale`. Article shares are proportionally constrained by
`news_score_clip`, so their sum matches the aggregate raw model input. When
the factor is off, the same location is explicitly labeled an unweighted
analysis scenario. Fitted-news mode marks per-article direction and magnitude
unavailable because the fitted Huber coefficient applies to the aggregated bar
feature. The tape also states whether the news factor is enabled. Its move is
not the strategy's total `yhat`, an expected trade return, or a recommendation.
The tape can be paused without stopping news collection or the strategy. If the
database has not been created, the dashboard shows an explicit waiting state
rather than demonstration headlines.

If the local LLM refines an article after the latest completed strategy bar,
the row distinguishes the latest analysis from the deterministic/LLM version
that actually contributed at that bar. Effective moves and connection drivers
always use that causal version. If the refinement removes a strategy-ticker
link, the causal connection remains visible and the row is marked
`LATEST UNLINKED`.

## Backtest and optimization

Research commands never contact news services. They freeze the captured
database to a content-addressed SQLite snapshot before evaluation:

```bash
python -m quant.run.run_backtest \
  --csv quant/data/equity_bars.csv --asset-class equity \
  --tickers SPY QQQ XLE XLK --news-db quant/data/news.sqlite3

python -m quant.optimize.optimize \
  --csv quant/data/equity_bars.csv --asset-class equity \
  --tickers SPY QQQ XLE XLK --news-db quant/data/news.sqlite3 --trials 40
```

Each bar can use an article only when all of these are true:

- `published_at` is no later than the bar;
- the first `received_at` is no later than the bar;
- an analysis version existed by the bar.

Deduplication never moves the first receipt time forward. Article bodies and
LLM refinements are stored as timestamped analysis versions, so a later result
cannot leak into an earlier bar. Optuna uses one immutable snapshot for every
fold, normal/stressed-cost rerun, and final holdout.

News captured today cannot honestly add signal to a backtest of older bars.
Build a forward archive first, then compare news-enabled and news-disabled
shadow/backtest results over the same interval. The feature is bounded and
fail-open, but it is not presumed to improve performance without that evidence.

## RSS catalog

The built-in catalog in `news/catalog.py` covers monetary policy, economic
indicators, securities regulation, energy, petroleum, natural gas, coal,
agriculture, food, healthcare, pharmaceuticals, cybersecurity, aerospace,
transport/logistics, technology, commodities, and broad business headlines.

Extend or replace it with JSON:

```json
{
  "replace_defaults": false,
  "feeds": [
    {
      "name": "Publisher name",
      "url": "https://example.com/feed.xml",
      "industries": ["semiconductors"],
      "commodities": ["copper"],
      "weight": 0.8
    }
  ]
}
```

Pass the file with `--news-rss-catalog` in `run_live` or `--rss-catalog` in
the standalone collector. Unknown taxonomy values are rejected rather than
silently entering the model.

IBKR API reference: [News](https://interactivebrokers.github.io/tws-api/news.html).
