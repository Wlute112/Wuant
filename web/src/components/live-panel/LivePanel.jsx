import { useEffect, useMemo, useRef, useState } from "react";

import { useInterval } from "../../hooks/useInterval.js";
import { api } from "../../lib/api.js";
import { formatNum, formatPct, formatTime, formatUsd } from "../../lib/format.js";
import ModelDecisionTape from "../model-tape/ModelDecisionTape.jsx";
import NewsTape from "../news-tape/NewsTape.jsx";
import DockWorkspace from "../workspace/DockWorkspace.jsx";
import "./live-panel.css";

const TICKER_PATTERN = /^[A-Z0-9][A-Z0-9.-]{0,14}$/;
const BAR_HOURS = [1, 2, 3, 4, 8, 24];

function normalizeTicker(value) {
  return String(value || "").trim().toUpperCase();
}

function finiteNonnegative(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}

function telemetryBarHours(value) {
  const text = String(value || "").toUpperCase();
  if (text.includes("DAY")) return 24;
  const suffixMatch = text.match(/-(\d+)-HOUR-/);
  if (suffixMatch) return Number(suffixMatch[1]);
  const compactMatch = text.match(/^(\d+)H$/);
  return compactMatch ? Number(compactMatch[1]) : null;
}

function mergeMarketAndModelBars(marketBars, modelPoints, barHours) {
  if (!marketBars.length) return modelPoints;
  if (!modelPoints.length) return marketBars;

  const exact = new Map(modelPoints.map((point) => [point.ts, point]));
  const timedModel = modelPoints
    .map((point) => ({ point, time: Date.parse(point.ts) }))
    .filter((item) => Number.isFinite(item.time));
  const tolerance = Number(barHours) * 60 * 60 * 1000 * 0.55;

  return marketBars.map((bar) => {
    let modelPoint = exact.get(bar.ts);
    if (!modelPoint) {
      const barTime = Date.parse(bar.ts);
      let nearest = null;
      for (const item of timedModel) {
        const distance = Math.abs(item.time - barTime);
        if (distance <= tolerance && (!nearest || distance < nearest.distance)) {
          nearest = { distance, point: item.point };
        }
      }
      modelPoint = nearest?.point;
    }
    return modelPoint ? { ...modelPoint, ...bar, model_ts: modelPoint.ts } : bar;
  });
}

function marketStatusCopy(feed, brokerStatus) {
  if (feed?.status === "streaming") {
    return feed.bars?.at(-1)?.complete === false
      ? "IB GATEWAY · CURRENT BAR UPDATING"
      : "IB GATEWAY · STREAMING";
  }
  if (["qualifying", "backfilling", "reconnecting"].includes(feed?.status)) {
    return `${String(feed.status).toUpperCase()} · IB GATEWAY`;
  }
  if (feed?.status === "error") return "IB MARKET DATA ERROR";
  if (feed?.status === "disconnected" || brokerStatus?.status !== "connected") {
    return "IB GATEWAY DISCONNECTED";
  }
  return "MARKET FEED READY";
}

function marketBasisCopy(feed, assetClass) {
  if (assetClass === "crypto") return "24/7 · MIDPOINT";
  if (feed?.price_adjustment === "split_adjusted_dividend_unadjusted") {
    return `${feed.session_scope === "all_hours" ? "ALL HOURS" : "RTH"} · SPLIT ADJ / DIVIDEND RAW`;
  }
  return feed?.session_scope === "all_hours" ? "ALL HOURS" : "RTH";
}

function formatAge(seconds) {
  if (!Number.isFinite(seconds)) return "UNKNOWN";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

function formatNsTimestamp(value) {
  const milliseconds = Number(value) / 1_000_000;
  return Number.isFinite(milliseconds) ? formatTime(new Date(milliseconds).toISOString()) : "—";
}

export default function LivePanel({
  mode = "paper",
  assetClass = "crypto",
  profile,
  brokerStatus = {},
  toolbarNavigation = null,
  toolbarLead = null,
  toolbarActions = null,
  toolbarStatus = null,
}) {
  const [telemetry, setTelemetry] = useState(null);
  const [ticker, setTicker] = useState(null);
  const [symbolInput, setSymbolInput] = useState("");
  const [barHours, setBarHours] = useState(1);
  const [marketFeed, setMarketFeed] = useState(null);
  const [marketError, setMarketError] = useState(null);
  const [marketRequestVersion, setMarketRequestVersion] = useState(0);
  const [feedStatus, setFeedStatus] = useState("loading");
  const [feedError, setFeedError] = useState(null);
  const [newsFeed, setNewsFeed] = useState(null);
  const [newsStatus, setNewsStatus] = useState("loading");
  const [newsError, setNewsError] = useState(null);
  const [newsPaused, setNewsPaused] = useState(false);
  const marketRequestRef = useRef(0);
  const barPeriodInitializedRef = useRef(false);

  async function refreshTelemetry() {
    try {
      const next = await api.getLiveTelemetry(assetClass, null, mode);
      setTelemetry(next);
      setFeedStatus("ready");
      setFeedError(null);
    } catch (error) {
      setFeedStatus(telemetry ? "stale" : "error");
      setFeedError(`Model telemetry unavailable: ${error.message}`);
    }
  }

  useEffect(() => {
    setTelemetry(null);
    setTicker(null);
    setSymbolInput("");
    setMarketFeed(null);
    setMarketError(null);
    setBarHours(24);
    barPeriodInitializedRef.current = false;
    setFeedStatus("loading");
    refreshTelemetry();
  }, [assetClass, mode]);
  useInterval(refreshTelemetry, 3000);

  const strategyBarHours = telemetryBarHours(telemetry?.bar_type);
  useEffect(() => {
    if (
      !barPeriodInitializedRef.current
      && strategyBarHours
      && BAR_HOURS.includes(strategyBarHours)
    ) {
      setBarHours(strategyBarHours);
      barPeriodInitializedRef.current = true;
    }
  }, [strategyBarHours]);

  const isDemo = Boolean(telemetry?.mock);
  const positions = isDemo ? [] : telemetry?.positions || [];
  const strategyTickers = telemetry?.tickers || Object.keys(telemetry?.series || {});
  const knownTickers = useMemo(
    () => Array.from(new Set([
      ...positions.map((position) => normalizeTicker(position.symbol)),
      ...strategyTickers.map(normalizeTicker),
    ].filter(Boolean))),
    [positions, strategyTickers.join("|")],
  );
  const newsRawScale = finiteNonnegative(telemetry?.model?.news_raw_scale, 0.001);
  const configuredNewsScoreClip = Number(telemetry?.model?.news_score_clip);
  const newsScoreClip = Number.isFinite(configuredNewsScoreClip)
    && configuredNewsScoreClip > 0
    ? Math.min(configuredNewsScoreClip, 1)
    : 1;
  const newsSource = telemetry?.model?.news_source === "fit" ? "fit" : "raw";
  const newsFactorEnabled = telemetry?.model?.use_news_features === true;
  const newsHalfLifeHours = Math.max(
    finiteNonnegative(telemetry?.model?.news_half_life_hours, 12),
    0.25,
  );
  const newsMaxAgeHours = Math.max(
    finiteNonnegative(telemetry?.model?.news_max_age_hours, 72),
    newsHalfLifeHours,
  );
  const newsDirectWeight = finiteNonnegative(telemetry?.model?.news_direct_weight, 1);
  const newsIndustryWeight = finiteNonnegative(telemetry?.model?.news_industry_weight, 0.45);
  const newsCommodityWeight = finiteNonnegative(telemetry?.model?.news_commodity_weight, 0.55);
  const newsMacroWeight = finiteNonnegative(telemetry?.model?.news_macro_weight, 0.2);
  const newsTickerKey = knownTickers.join("|");
  const newsFactorAsOf = useMemo(() => {
    let latest = null;
    let latestMs = -Infinity;
    for (const symbol of knownTickers) {
      const timestamp = telemetry?.series?.[symbol]?.at(-1)?.ts;
      const milliseconds = Date.parse(timestamp);
      if (Number.isFinite(milliseconds) && milliseconds > latestMs) {
        latest = timestamp;
        latestMs = milliseconds;
      }
    }
    return latest;
  }, [newsTickerKey, telemetry]);

  async function refreshNews() {
    if (newsPaused) return;
    try {
      const next = await api.getLiveNews({
        tickers: knownTickers,
        jobId: telemetry?.job_id || null,
        newsRawScale,
        newsScoreClip,
        newsSource,
        factorEnabled: newsFactorEnabled,
        factorAsOf: newsFactorAsOf,
        halfLifeHours: newsHalfLifeHours,
        maxAgeHours: newsMaxAgeHours,
        directWeight: newsDirectWeight,
        industryWeight: newsIndustryWeight,
        commodityWeight: newsCommodityWeight,
        macroWeight: newsMacroWeight,
        limit: 80,
      });
      setNewsFeed(next);
      setNewsStatus("ready");
      setNewsError(null);
    } catch (error) {
      setNewsStatus(newsFeed ? "stale" : "error");
      setNewsError(`News feed unavailable: ${error.message}`);
    }
  }

  useEffect(() => {
    setNewsFeed(null);
    setNewsStatus("loading");
    setNewsError(null);
    refreshNews();
  }, [
    assetClass,
    mode,
    newsRawScale,
    newsScoreClip,
    newsSource,
    newsFactorEnabled,
    newsFactorAsOf,
    newsHalfLifeHours,
    newsMaxAgeHours,
    newsDirectWeight,
    newsIndustryWeight,
    newsCommodityWeight,
    newsMacroWeight,
    newsTickerKey,
  ]);
  useInterval(refreshNews, newsPaused ? null : 2000);

  useEffect(() => {
    if (!ticker && knownTickers.length) {
      setTicker(knownTickers[0]);
      setSymbolInput(knownTickers[0]);
    }
  }, [knownTickers.join("|"), ticker]);

  const includeExtendedHours = Boolean(telemetry?.include_extended_hours);

  useEffect(() => {
    if (!ticker) return undefined;
    let active = true;
    const requestId = ++marketRequestRef.current;
    setMarketError(null);
    setMarketFeed((current) => current && current.symbol === ticker
      ? { ...current, status: "qualifying" }
      : { status: "qualifying", symbol: ticker, bars: [] });

    api.subscribeBrokerBars({
      symbol: ticker,
      asset_class: assetClass,
      bar_hours: Number(barHours),
      include_extended_hours: assetClass === "equity" && includeExtendedHours,
    }).then((next) => {
      if (!active || requestId !== marketRequestRef.current) return;
      setMarketFeed(next);
      setMarketError(next.error || null);
    }).catch((error) => {
      if (!active || requestId !== marketRequestRef.current) return;
      setMarketError(error.status === 404
        ? "Live-bar API route unavailable. Restart ./quant to load the updated backend."
        : `Could not load ${ticker} bars: ${error.message}`);
      setMarketFeed({ status: "error", symbol: ticker, bars: [] });
    });

    return () => {
      active = false;
    };
  }, [assetClass, barHours, includeExtendedHours, marketRequestVersion, ticker]);

  async function refreshMarketBars() {
    if (!ticker) return;
    const requestId = ++marketRequestRef.current;
    try {
      const next = await api.getBrokerBars(
        ticker,
        assetClass,
        Number(barHours),
        assetClass === "equity" && includeExtendedHours,
      );
      if (requestId !== marketRequestRef.current) return;
      setMarketFeed(next);
      setMarketError(next.error || null);
    } catch (error) {
      if (requestId !== marketRequestRef.current) return;
      setMarketError(error.status === 404
        ? "Live-bar API route unavailable. Restart ./quant to load the updated backend."
        : `Live ${ticker} bars unavailable: ${error.message}`);
    }
  }
  useInterval(refreshMarketBars, ticker ? 2000 : null);

  function selectTicker(symbol) {
    const normalized = normalizeTicker(symbol);
    setTicker(normalized);
    setSymbolInput(normalized);
    setMarketError(null);
  }

  function submitTicker(event) {
    event.preventDefault();
    const normalized = normalizeTicker(symbolInput);
    if (!TICKER_PATTERN.test(normalized)) {
      setMarketError("Enter a valid ticker using 1–15 letters, numbers, dots, or hyphens.");
      return;
    }
    if (normalized === ticker) {
      setMarketRequestVersion((version) => version + 1);
    } else {
      selectTicker(normalized);
    }
  }

  const realModelPoints = isDemo ? [] : telemetry?.series?.[ticker] || [];
  const fallbackPoints = telemetry?.series?.[ticker] || [];
  const marketBars = marketFeed?.bars || [];
  const timeframeMatches = !strategyBarHours || strategyBarHours === Number(barHours);
  const points = useMemo(
    () => marketBars.length
      ? mergeMarketAndModelBars(
        marketBars,
        timeframeMatches ? realModelPoints : [],
        barHours,
      )
      : fallbackPoints,
    [barHours, fallbackPoints, marketBars, realModelPoints, timeframeMatches],
  );
  const selectedPosition = isDemo && marketBars.length
    ? null
    : positions.find((position) => normalizeTicker(position.symbol) === ticker) || null;
  const risk = telemetry?.risk || {};
  const session = risk.session || {};
  const orders = isDemo ? [] : risk.orders || [];
  const fills = isDemo ? [] : risk.fills || [];
  const operatorAlerts = isDemo ? [] : risk.operator_alerts || [];
  const sessionRunning = !isDemo
    && telemetry?.status === "running"
    && telemetry?.job_status === "running";
  const isLastKnown = Boolean(telemetry)
    && !isDemo
    && !sessionRunning
    && telemetry.status !== "connecting";
  const statusUnknown = feedStatus === "error"
    || isDemo
    || isLastKnown
    || (!telemetry && feedStatus !== "loading");
  const executionUncertain = !isDemo
    && sessionRunning
    && (risk.entries_allowed !== true || risk.reconciliation_state === "UNCERTAIN");
  const isConnecting = telemetry?.status === "connecting" || feedStatus === "loading";
  const marketOnly = marketBars.length > 0 && realModelPoints.length === 0;
  const chartMock = isDemo && marketBars.length === 0;
  const modelOverlayActive = chartMock
    || (realModelPoints.length > 0 && (!marketBars.length || timeframeMatches));
  const chartSource = marketBars.length
    ? "IB Gateway"
    : chartMock
      ? "demonstration"
      : "strategy telemetry";
  const model = realModelPoints.length || chartMock ? telemetry?.model || {} : {};
  const scoringPoints = marketOnly
    ? []
    : realModelPoints.length
      ? realModelPoints
      : chartMock
        ? fallbackPoints
        : [];
  const latestModelPoint = [...scoringPoints]
    .reverse()
    .find((point) => point.predicted_return != null || point.yhat != null)
    || scoringPoints.at(-1)
    || null;
  const workspaceStatus = (
    <span
      className={`live-panel__workspace-status ${executionUncertain || statusUnknown ? "is-unsafe" : sessionRunning ? "is-running" : ""}`}
      role="status"
      aria-live="polite"
    >
      {isConnecting
        ? "CONNECTING"
        : executionUncertain
          ? "EXECUTION LOCKED"
          : isLastKnown
            ? "LAST KNOWN"
            : sessionRunning
              ? "AUTOMATION RUNNING"
              : isDemo
                ? "DEMONSTRATION"
                : "SESSION OFF"}
    </span>
  );
  const panels = [
    {
      id: "chart",
      title: "Live chart / model tape",
      kind: "chart",
      reading: `${ticker || knownTickers[0] || "—"} · ${barHours === 24 ? "1D" : `${barHours}H`}`,
      defaultLayout: { x: 0, y: 0, w: 8, h: 7 },
      minW: 4,
      minH: 4,
      content: (
        <div className="live-panel__chart-module">
          <div className="live-panel__market-console">
            <form className="live-panel__symbol-search" onSubmit={submitTicker}>
              <label htmlFor={`live-chart-symbol-${mode}`}>
                <span className="label">Symbol</span>
                <input
                  id={`live-chart-symbol-${mode}`}
                  value={symbolInput}
                  list={`known-live-symbols-${mode}`}
                  maxLength={15}
                  pattern="[A-Za-z0-9][A-Za-z0-9.\-]{0,14}"
                  autoComplete="off"
                  spellCheck="false"
                  placeholder={assetClass === "equity" ? "SPY" : "BTC"}
                  onChange={(event) => setSymbolInput(event.target.value.toUpperCase())}
                />
              </label>
              <datalist id={`known-live-symbols-${mode}`}>
                {knownTickers.map((symbol) => <option key={symbol} value={symbol} />)}
              </datalist>
              <label htmlFor={`live-chart-period-${mode}`}>
                <span className="label">Bars</span>
                <select
                  id={`live-chart-period-${mode}`}
                  value={barHours}
                  onChange={(event) => setBarHours(Number(event.target.value))}
                >
                  {BAR_HOURS.map((hours) => (
                    <option key={hours} value={hours}>
                      {hours === 24 ? "1 day" : `${hours} hour${hours === 1 ? "" : "s"}`}
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" className="live-panel__load-bars">Load</button>
            </form>
            <div className="live-panel__known-symbols" aria-label="Strategy and position tickers">
              {knownTickers.map((symbol) => (
                <button
                  key={symbol}
                  type="button"
                  className={ticker === symbol ? "is-active" : ""}
                  aria-pressed={ticker === symbol}
                  onClick={() => selectTicker(symbol)}
                >
                  {symbol}
                </button>
              ))}
            </div>
            <div className="live-panel__chart-status" role="status" aria-live="polite">
              <span className={`label ${marketFeed?.status === "error" || marketFeed?.status === "disconnected" ? "is-error" : ""}`}>
                {marketStatusCopy(marketFeed, brokerStatus)}
              </span>
              <span className="label">{marketBasisCopy(marketFeed, assetClass)}</span>
              <span className={`label ${marketOnly ? "is-market-only" : ""}`}>
                {marketOnly
                  ? "MARKET ONLY"
                  : realModelPoints.length && !timeframeMatches
                    ? `MODEL ${strategyBarHours === 24 ? "1D" : `${strategyBarHours}H`} · REFERENCES ONLY`
                    : realModelPoints.length
                      ? "MODEL OVERLAY ACTIVE"
                      : chartMock
                        ? "DEMO OVERLAY"
                        : "MODEL WARMING"}
              </span>
            </div>
          </div>
          {marketError && <div className="live-panel__market-error" role="alert">{marketError}</div>}
          <ModelDecisionTape
            points={points}
            ticker={ticker || knownTickers[0] || "—"}
            model={model}
            position={selectedPosition}
            assetClass={assetClass}
            live
            mock={chartMock}
            marketOnly={marketOnly}
            marketSource={chartSource}
            modelOverlayActive={modelOverlayActive}
            modelBarHours={strategyBarHours}
          />
        </div>
      ),
    },
    {
      id: "model-score",
      title: "Model score",
      kind: "model-score",
      reading: latestModelPoint?.signal || "WARMING",
      defaultLayout: { x: 8, y: 0, w: 2, h: 3 },
      minW: 2,
      minH: 2,
      content: <ModelScore point={latestModelPoint} model={model} ticker={ticker} />,
    },
    {
      id: "risk",
      title: "Risk rails",
      kind: "risk",
      reading: risk.state || "UNKNOWN",
      defaultLayout: { x: 10, y: 0, w: 2, h: 3 },
      minW: 2,
      minH: 2,
      content: <RiskReadout risk={risk} statusUnknown={statusUnknown} isDemo={isDemo} />,
    },
    {
      id: "telemetry",
      title: "Live telemetry",
      kind: "telemetry",
      reading: String(brokerStatus.status || "UNKNOWN").toUpperCase(),
      defaultLayout: { x: 8, y: 3, w: 4, h: 2 },
      minW: 3,
      minH: 2,
      content: (
        <TelemetryReadout
          brokerStatus={brokerStatus}
          risk={risk}
          session={session}
          feedError={feedError}
          operatorAlerts={operatorAlerts}
          isDemo={isDemo}
        />
      ),
    },
    {
      id: "news",
      title: "News score / model impact",
      kind: "news",
      reading: `${newsFeed?.items?.length || 0} HEADLINES`,
      defaultLayout: { x: 8, y: 5, w: 4, h: 7 },
      minW: 3,
      minH: 3,
      content: (
        <NewsTape
          feed={newsFeed}
          status={newsStatus}
          error={newsError}
          paused={newsPaused}
          active={sessionRunning}
          factorEnabled={newsFactorEnabled}
          embedded
          onTogglePause={() => setNewsPaused((value) => !value)}
          onSelectTicker={selectTicker}
        />
      ),
    },
    {
      id: "positions",
      title: "Portfolio / positions",
      kind: "positions",
      reading: `${positions.length} OPEN`,
      defaultLayout: { x: 0, y: 7, w: 8, h: 2 },
      minW: 4,
      minH: 2,
      content: (
        <PositionsTable
          positions={positions}
          ticker={ticker}
          assetClass={assetClass}
          isDemo={isDemo}
          statusUnknown={statusUnknown}
          isConnecting={isConnecting}
          onSelectTicker={selectTicker}
        />
      ),
    },
    {
      id: "activity",
      title: "Model / execution actions",
      kind: "activity",
      reading: `${orders.length} ORDERS · ${fills.length} FILLS`,
      defaultLayout: { x: 0, y: 9, w: 8, h: 3 },
      minW: 4,
      minH: 2,
      content: (
        <ActivityTape
          series={isDemo && marketBars.length ? {} : telemetry?.series || {}}
          orders={orders}
          fills={fills}
          assetClass={assetClass}
          isDemo={isDemo}
        />
      ),
    },
  ];

  return (
    <DockWorkspace
      key={`${mode}-${assetClass}`}
      workspaceId={`${mode}-${assetClass}`}
      title={`${mode === "live" ? "Live" : "Paper"} trading · ${profile?.short_label || assetClass}`}
      subtitle={`${telemetry?.include_extended_hours ? "Regular + extended hours" : profile?.market?.session || "selected session"} · ${profile?.market?.venue || "IBKR"} · ${profile?.scoring?.label || "risk-adjusted objective"}`}
      status={workspaceStatus}
      panels={panels}
      toolbarNavigation={toolbarNavigation}
      toolbarLead={toolbarLead}
      toolbarActions={toolbarActions}
      toolbarStatus={toolbarStatus}
    />
  );

}

function ModelScore({ point, model, ticker }) {
  const yhat = point?.predicted_return ?? point?.yhat;
  const threshold = point?.entry_threshold ?? model?.entry_threshold;
  const conviction = Number.isFinite(yhat) && Number.isFinite(threshold) && threshold > 0
    ? Math.abs(yhat) / threshold
    : null;
  const signal = point?.signal || (point?.trained === false ? "WARMUP" : "NOT RUNNING");
  const newsContribution = Number.isFinite(point?.news_score) && model?.news_source === "raw"
    ? point.news_score * Number(model.news_raw_scale || 0)
    : null;
  return (
    <div className="live-panel__model-score">
      <div className={`live-panel__decision is-${String(signal).toLowerCase()}`}>
        <span>{ticker || "MODEL"}</span>
        <strong>{signal}</strong>
      </div>
      <dl>
        <div><dt>Forecast ŷ</dt><dd>{Number.isFinite(yhat) ? formatPct(yhat * 100, 3) : "WARMING"}</dd></div>
        <div><dt>Entry threshold</dt><dd>{Number.isFinite(threshold) ? formatPct(threshold * 100, 3) : "—"}</dd></div>
        <div><dt>Conviction</dt><dd>{Number.isFinite(conviction) ? `${formatNum(conviction, 2)}×` : "—"}</dd></div>
        <div><dt>Predicted close</dt><dd>{Number.isFinite(point?.predicted_price) ? formatUsd(point.predicted_price) : "—"}</dd></div>
        <div><dt>News score</dt><dd>{Number.isFinite(point?.news_score) ? formatNum(point.news_score, 3) : "—"}</dd></div>
        <div><dt>News to ŷ</dt><dd>{Number.isFinite(newsContribution) ? formatPct(newsContribution * 100, 3) : model?.news_source === "fit" ? "FIT" : "—"}</dd></div>
        <div><dt>HMM / regime</dt><dd>{point?.hmm_label || point?.state_label || "WARMING"}</dd></div>
        <div><dt>Bull / bear</dt><dd>{Number.isFinite(point?.p_bull) ? `${formatPct(point.p_bull * 100, 0)} / ${formatPct((point.p_bear || 0) * 100, 0)}` : "—"}</dd></div>
      </dl>
    </div>
  );
}

function RiskReadout({ risk, statusUnknown, isDemo }) {
  const rails = risk.rails || {};
  return (
    <div className="live-panel__risk-readout">
      <dl>
        <div><dt>State</dt><dd>{risk.state || "UNKNOWN"}</dd></div>
        <div><dt>Allocation</dt><dd>{Number.isFinite(risk.equity) ? formatUsd(risk.equity) : "UNKNOWN"}</dd></div>
        <div><dt>Daily PnL</dt><dd className={Number.isFinite(risk.daily_pnl_pct) && risk.daily_pnl_pct < 0 ? "is-negative" : "is-positive"}>{Number.isFinite(risk.daily_pnl_pct) ? formatPct(risk.daily_pnl_pct) : "UNKNOWN"}</dd></div>
        <div><dt>Drawdown / kill</dt><dd>{Number.isFinite(risk.drawdown_pct) ? `${formatPct(risk.drawdown_pct)} / ${rails.kill_switch_pct ?? "—"}%` : "UNKNOWN"}</dd></div>
        <div><dt>Leverage / max</dt><dd>{Number.isFinite(risk.gross_leverage) ? `${formatNum(risk.gross_leverage, 2)}× / ${rails.leverage_max ?? "—"}×` : "UNKNOWN"}</dd></div>
        <div><dt>Trade risk</dt><dd>{rails.risk_budget_pct ?? "—"}% / {rails.hard_cap_pct ?? "—"}% cap</dd></div>
        <div><dt>Daily halt</dt><dd>{rails.daily_loss_limit_pct ?? "—"}%</dd></div>
      </dl>
      <div className={`live-panel__kill-switch ${statusUnknown ? "is-unknown" : risk.kill_switch_engaged ? "is-engaged" : ""}`}>
        KILL {isDemo ? "DEMO" : statusUnknown ? "UNKNOWN" : risk.kill_switch_engaged ? "ENGAGED" : "CLEAR"}
      </div>
    </div>
  );
}

function TelemetryReadout({ brokerStatus, risk, session, feedError, operatorAlerts, isDemo }) {
  return (
    <div className="live-panel__telemetry-readout">
      {feedError && <div className="live-panel__feed-error" role="alert">{feedError}</div>}
      <div className="live-panel__authority" aria-label="Authoritative broker and execution state">
        <AuthorityItem label="Broker" value={isDemo ? "NO ACTIVE BROKER" : String(brokerStatus.status || "UNKNOWN").toUpperCase()} unsafe={!isDemo && brokerStatus.status !== "connected"} />
        <AuthorityItem label="Reconcile" value={isDemo ? "DEMO" : risk.reconciliation_state || "UNKNOWN"} unsafe={!isDemo && risk.reconciliation_state !== "STRATEGY_CACHE_RECONCILED"} />
        <AuthorityItem label="Execution" value={isDemo ? "OFF" : risk.execution_state || "UNKNOWN"} unsafe={!isDemo && risk.execution_state !== "ACTIVE"} />
        <AuthorityItem label="Entries" value={isDemo ? "OFF" : risk.entries_allowed === true ? "ENABLED" : "FROZEN"} unsafe={!isDemo && risk.entries_allowed !== true} />
        <AuthorityItem label="Session" value={session.phase || (isDemo ? "DEMO" : "UNKNOWN")} unsafe={!isDemo && ["UNKNOWN", "HALTED", "STALE"].includes(session.phase)} />
        <AuthorityItem label="Data age" value={formatAge(session.data_age_seconds)} unsafe={!isDemo && !Number.isFinite(session.data_age_seconds)} />
        <AuthorityItem label="Next open" value={formatTime(session.next_open)} unsafe={!isDemo && !session.next_open} />
        <AuthorityItem label="Next close" value={formatTime(session.next_close)} unsafe={!isDemo && !session.next_close} />
      </div>
      {!isDemo && operatorAlerts.length > 0 && (
        <div className="live-panel__alerts" role="alert">
          {[...operatorAlerts].reverse().slice(0, 3).map((alert, index) => (
            <span key={`${alert.ts_ns}-${alert.code}-${index}`}>{alert.severity} · {alert.code} · {alert.message}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function PositionsTable({ positions, ticker, assetClass, isDemo, statusUnknown, isConnecting, onSelectTicker }) {
  return (
    <div className="live-panel__positions">
      <table className="live-panel__table">
        <caption className="sr-only">Current broker positions for the selected trading profile</caption>
        <thead><tr><th>Symbol</th><th>Side</th><th>Quantity</th><th>Mark</th><th>Change</th><th>Market value</th><th>Gain/Loss</th><th>Broker protection</th></tr></thead>
        <tbody>
          {positions.map((position) => {
            const change = position.change_pct ?? (position.avg_price ? ((position.mark_price - position.avg_price) / position.avg_price) * 100 : 0);
            const pnl = position.gain_loss_total ?? position.unrealized_pnl;
            return (
              <tr key={position.symbol} className={normalizeTicker(position.symbol) === ticker ? "is-charted" : ""}>
                <td><button type="button" className="live-panel__symbol-link" onClick={() => onSelectTicker(position.symbol)}>{position.symbol}</button></td>
                <td>{position.side || "—"}</td>
                <td className="num">{formatNum(position.qty, assetClass === "equity" ? 0 : 6)}</td>
                <td className="num">{formatUsd(position.mark_price)}</td>
                <td className={`num ${change >= 0 ? "is-positive" : "is-negative"}`}>{formatPct(change)}</td>
                <td className="num">{formatUsd(position.market_value ?? position.notional ?? position.qty * position.mark_price)}</td>
                <td className={`num ${finitePnlClass(pnl)}`}>{formatUsd(pnl)}</td>
                <td className={position.protection_guaranteed ? "is-positive" : "is-negative"}>{position.protection_guaranteed ? `OCA ACTIVE · ${formatNum(position.protection_quantity, assetClass === "equity" ? 0 : 6)}` : `NOT GUARANTEED · ${String(position.reference_status || "UNKNOWN").toUpperCase()}`}</td>
              </tr>
            );
          })}
          {isDemo && !positions.length && <tr><td colSpan={8} className="live-panel__empty">No active paper/live strategy</td></tr>}
          {!isDemo && statusUnknown && !positions.length && <tr><td colSpan={8} className="live-panel__empty is-error">Position status unknown</td></tr>}
          {!isDemo && !statusUnknown && !positions.length && <tr><td colSpan={8} className="live-panel__empty">{isConnecting ? "Waiting for broker reconciliation…" : "No open positions"}</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function ActivityTape({ series, orders, fills, assetClass, isDemo }) {
  const decisions = Object.entries(series).flatMap(([symbol, points]) => (points || []).slice(-6).map((point) => ({
    key: `model-${symbol}-${point.ts}`,
    time: Date.parse(point.ts) || 0,
    timeLabel: formatTime(point.ts),
    source: "MODEL",
    symbol,
    action: point.signal || "WARMUP",
    detail: Number.isFinite(point.predicted_return ?? point.yhat) ? `ŷ ${formatPct((point.predicted_return ?? point.yhat) * 100, 3)} · NEWS ${Number.isFinite(point.news_score) ? formatNum(point.news_score, 2) : "—"}` : "MODEL WARMUP",
  })));
  const fillEvents = fills.slice(-12).map((fill) => ({
    key: `fill-${fill.execution_id}`,
    time: Number(fill.ts_ns) / 1_000_000,
    timeLabel: formatNsTimestamp(fill.ts_ns),
    source: "FILL",
    symbol: String(fill.instrument_id || "—").split(".")[0],
    action: fill.side,
    detail: `${formatNum(fill.quantity, assetClass === "equity" ? 0 : 6)} @ ${formatUsd(fill.price)}`,
  }));
  const events = [...decisions, ...fillEvents].sort((a, b) => b.time - a.time).slice(0, 30);
  return (
    <div className="live-panel__activity">
      <table className="live-panel__table live-panel__activity-table">
        <thead><tr><th>Time</th><th>Source</th><th>Symbol</th><th>Action</th><th>Decision / execution detail</th></tr></thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.key}>
              <td>{event.timeLabel}</td><td>{event.source}</td><td>{event.symbol}</td>
              <td className={event.action === "BUY" ? "is-positive" : ["SELL", "EXIT"].includes(event.action) ? "is-negative" : ""}>{event.action}</td>
              <td>{event.detail}</td>
            </tr>
          ))}
          {!events.length && <tr><td colSpan={5} className="live-panel__empty">{isDemo ? "Demonstration model is waiting" : "No model or execution actions recorded"}</td></tr>}
        </tbody>
      </table>
      {orders.length > 0 && (
        <div className="live-panel__order-ribbon" aria-label="Latest broker order states">
          {[...orders].reverse().slice(0, 6).map((order) => (
            <span key={order.client_order_id} className={order.rejection_reason ? "is-negative" : ""}>{order.role} · {order.side} · {order.status} · {formatNum(order.filled_quantity, assetClass === "equity" ? 0 : 6)}/{formatNum(order.quantity, assetClass === "equity" ? 0 : 6)}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function AuthorityItem({ label, value, unsafe = false }) {
  return (
    <div className={`live-panel__authority-item ${unsafe ? "is-unsafe" : ""}`}>
      <span className="label">{label}</span>
      <span className="num">{value}</span>
    </div>
  );
}

function finitePnlClass(value) {
  if (!Number.isFinite(value)) return "";
  return value >= 0 ? "is-positive" : "is-negative";
}
