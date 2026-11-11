import { useEffect, useMemo, useRef, useState } from "react";

import { normalizeChartPoints, chartWindow, CHART_DEFAULTS } from "../../lib/chartView.js";

import { formatNum, formatPct, formatUsd } from "../../lib/format.js";
import "./model-decision-tape.css";

const WIDTH = 1200;
const HEIGHT = 560;
const MARGIN = { left: 56, right: 144, top: 24 };
const PRICE_BOTTOM = 350;
const REGIME_TOP = 392;
const REGIME_BOTTOM = 492;
const MAX_VISIBLE_POINTS = 150;
const MIN_CHART_WIDTH = 760;
// The compact live header overlays the canvas. Keep the SVG viewBox in the
// same aspect ratio as the visible plot beneath it so the chart uses the full
// dock width rather than being letterboxed at either side.
const LIVE_PLOT_HEADER_HEIGHT = 24;

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function linePath(points, x, y, key) {
  let path = "";
  let active = false;
  points.forEach((point, index) => {
    const value = point[key];
    if (!finite(value)) {
      active = false;
      return;
    }
    path += `${active ? "L" : "M"}${x(index).toFixed(2)},${y(value).toFixed(2)}`;
    active = true;
  });
  return path;
}

function mostRecentPoint(points, ...keys) {
  for (let index = points.length - 1; index >= 0; index -= 1) {
    for (const key of keys) {
      if (finite(points[index]?.[key])) return points[index];
    }
  }
  return null;
}

function stateClass(point) {
  const state = String(point.hmm_label || point.state_label || "sideways").toLowerCase();
  if (state.includes("bull")) return "is-bull";
  if (state.includes("bear")) return "is-bear";
  return "is-sideways";
}

function forecastBar(point) {
  const supplied = point.forecast;
  if (
    supplied
    && finite(supplied.open)
    && finite(supplied.high)
    && finite(supplied.low)
    && finite(supplied.close)
  ) {
    return supplied;
  }
  if (!finite(point.predicted_price) || !finite(point.close)) return null;
  const envelope = finite(point.atr) ? point.atr * 0.5 : 0;
  return {
    horizon_bars: 1,
    open: point.close,
    close: point.predicted_price,
    high: Math.max(point.close, point.predicted_price) + envelope,
    low: Math.min(point.close, point.predicted_price) - envelope,
    basis: "huber_close_atr_envelope",
  };
}

function dateLabel(ts, assetClass, sessionDate = null) {
  if (!ts) return "—";
  const value = sessionDate ? `${sessionDate}T12:00:00Z` : ts;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    ...(sessionDate ? { year: "numeric" } : {}),
    ...(assetClass === "equity" && !sessionDate ? { hour: "numeric" } : {}),
    ...(sessionDate ? { timeZone: "UTC" } : {}),
  }).format(new Date(value));
}

function pointTimestampLabel(point, assetClass) {
  if (point?.session_date) {
    return `${dateLabel(point.ts, assetClass, point.session_date)} trading session`;
  }
  return point?.ts ? new Date(point.ts).toLocaleString() : "—";
}

export default function ModelDecisionTape({
  points = [],
  ticker,
  model = {},
  position = null,
  assetClass = "crypto",
  live = false,
  mock = false,
  marketOnly = false,
  marketSource = "recorded bars",
  marketUpdateMode = null,
  modelOverlayActive = true,
  modelBarHours = null,
  chartSettings = null,
  toolbar = null,
  barHours = null,
}) {
  const settings = chartSettings || { ...CHART_DEFAULTS, forecasts: true, context: true };
  const [count, setCount] = useState(100);
  const [endTime, setEndTime] = useState(null);
  const [manualScale, setManualScale] = useState(1);
  const [cursorY, setCursorY] = useState(null);
  const dragRef = useRef(null);
  const [hovered, setHovered] = useState(null);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const canvasRef = useRef(null);
  const allData = useMemo(
    () => normalizeChartPoints(points),
    [points],
  );
  const window = chartWindow(allData.length, count, endTime, allData);
  const data = live ? allData.slice(window.start, window.end) : allData.slice(-MAX_VISIBLE_POINTS);
  const following = endTime == null;
  function pan(bars) {
    const end = Math.max(Math.min(count, allData.length), Math.min(allData.length, window.end + bars));
    setEndTime(Date.parse(allData[Math.max(0, end - 1)]?.ts));
    setHovered(null);
  }
  function zoom(factor, anchor = 0.5) {
    const next = Math.max(5, Math.min(Math.max(5, allData.length), Math.round(count * factor)));
    const end = Math.max(1, Math.min(allData.length, Math.round(window.end + (next - count) * (1 - anchor))));
    setEndTime(Date.parse(allData[end - 1]?.ts));
    setCount(next);
    setHovered(null);
  }
  useEffect(() => { setEndTime(null); setHovered(null); setCount(100); setManualScale(1); }, [ticker, barHours]);

  useEffect(() => {
    const node = canvasRef.current;
    if (!node) return undefined;
    const updateSize = () => {
      const bounds = node.getBoundingClientRect();
      setCanvasSize({ width: bounds.width, height: bounds.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(node);
    return () => observer.disconnect();
  }, [data.length > 0]);

  useEffect(() => {
    const node = canvasRef.current;
    if (!live || !node) return;
    const preventPageScroll = event => event.preventDefault();
    node.addEventListener('wheel', preventPageScroll, { passive: false });
    return () => node.removeEventListener('wheel', preventPageScroll);
  }, [live, data.length > 0]);

  function jumpToLatest() {
    setEndTime(null);
    setHovered(null);
    if (canvasRef.current) canvasRef.current.scrollLeft = canvasRef.current.scrollWidth;
  }

  useEffect(() => {
    if (!live) jumpToLatest();
  }, [ticker, points.length, live]);

  if (!data.length) {
    return (
      <section className="model-tape is-empty" aria-label={`Model decision tape for ${ticker || "instrument"}`}>
        {live && <div className="live-chart__controls" role="toolbar" aria-label="Chart controls">{toolbar}</div>}
        <div>
          <span className="label">Model decision tape</span>
          <p>{live ? "Enter a ticker to load current IB Gateway bars." : "This run has no model-chart points."}</p>
        </div>
      </section>
    );
  }

  const forecasts = data.map(point => !live || settings.forecasts ? forecastBar(point) : null);
  const maxForecastHorizon = forecasts.reduce(
    (maximum, forecast) => Math.max(maximum, Number(forecast?.horizon_bars) || 0),
    0,
  );
  const horizontalSlots = Math.max(1, data.length - 1 + maxForecastHorizon);
  const referencePoint = mostRecentPoint(data, "stop_loss", "stop_reference");
  const stop = live && !settings.levels ? null : position?.stop_loss
    ?? referencePoint?.stop_loss
    ?? referencePoint?.stop_reference
    ?? null;
  const target = live && !settings.levels ? null : position?.take_profit
    ?? referencePoint?.take_profit
    ?? referencePoint?.take_profit_reference
    ?? null;
  const entryPrice = live && !settings.levels ? null : position?.avg_price ?? null;
  const showModelPane = !marketOnly && (!live || (settings.regime && modelOverlayActive));
  const chartHeight = showModelPane ? HEIGHT : PRICE_BOTTOM + 64;
  const visiblePlotHeight = Math.max(
    0,
    canvasSize.height - (live ? LIVE_PLOT_HEADER_HEIGHT : 0),
  );
  const measuredAspect = visiblePlotHeight > 0 ? canvasSize.width / visiblePlotHeight : 0;
  const chartWidth = measuredAspect > 0
    ? Math.max(live ? 320 : MIN_CHART_WIDTH, chartHeight * measuredAspect)
    : WIDTH;
  const rightMargin = finite(entryPrice) || finite(stop) || finite(target) ? MARGIN.right : 64;
  const plotWidth = chartWidth - MARGIN.left - rightMargin;
  const candleWidth = Math.max(2, Math.min(7, (plotWidth / (horizontalSlots + 1)) * 0.58));
  const priceValues = [
    ...data.flatMap((point) => [point.low ?? point.close, point.high ?? point.close, ...(!live || settings.forecasts ? [point.predicted_price] : [])]),
    ...forecasts.flatMap((forecast) => forecast ? [forecast.low, forecast.high] : []),
  ].filter(finite);
  if (finite(stop)) priceValues.push(stop);
  if (finite(target)) priceValues.push(target);
  if (finite(entryPrice)) priceValues.push(entryPrice);
  let minPrice = Math.min(...priceValues);
  let maxPrice = Math.max(...priceValues);
  const pricePadding = Math.max((maxPrice - minPrice) * 0.08, Math.abs(maxPrice) * 0.0005, 0.01);
  const midpoint = (minPrice + maxPrice) / 2;
  const halfRange = ((maxPrice - minPrice) / 2 + pricePadding) * manualScale;
  minPrice = midpoint - halfRange;
  maxPrice = midpoint + halfRange;
  const x = (index) => MARGIN.left + (data.length === 1 && maxForecastHorizon === 0
    ? plotWidth / 2
    : (index / horizontalSlots) * plotWidth);
  const logarithmic = live && settings.logarithmic && minPrice > 0;
  const transformPrice = value => logarithmic ? Math.log(Math.max(value, minPrice)) : value;
  const yPrice = (value) => MARGIN.top + ((transformPrice(maxPrice) - transformPrice(value)) / Math.max(transformPrice(maxPrice) - transformPrice(minPrice), 1e-9)) * (PRICE_BOTTOM - MARGIN.top);
  const yProb = (value) => REGIME_BOTTOM - Math.max(0, Math.min(1, value ?? 0)) * (REGIME_BOTTOM - REGIME_TOP);
  const latest = data[data.length - 1];
  const latestDecision = [...data].reverse().find(
    (point) => finite(point.predicted_return) || finite(point.yhat),
  ) || latest;
  const active = hovered == null ? latestDecision : data[Math.min(hovered, data.length - 1)];
  const yhat = active.predicted_return ?? active.yhat;
  const threshold = active.entry_threshold ?? model.entry_threshold;
  const hmmWindow = model.hmm_train_window || latest.hmm_train_window || model.regime_window || latest.regime_window;
  const boundaryIndex = hmmWindow && hmmWindow < data.length ? data.length - hmmWindow : null;
  const hmmContext = hmmWindow ? allData.slice(-hmmWindow) : allData;
  const hmmLoaded = hmmWindow ? Math.min(allData.length, hmmWindow) : allData.length;
  const hmmVisible = hmmWindow ? Math.min(data.length, hmmWindow) : data.length;
  const hmmStartIndex = Math.max(0, data.length - hmmVisible);
  const observedSpacing = data.length > 1 ? x(1) - x(0) : candleWidth * 2;
  const hmmBoxStart = hmmStartIndex === 0
    ? MARGIN.left
    : (x(hmmStartIndex - 1) + x(hmmStartIndex)) / 2;
  const observedEnd = Math.min(
    chartWidth - rightMargin,
    x(data.length - 1) + observedSpacing / 2,
  );
  const entryTime = position?.entry_ts ? Date.parse(position.entry_ts) : null;
  const firstBarAfterEntry = Number.isFinite(entryTime)
    ? data.findIndex((point) => Date.parse(point.ts) >= entryTime)
    : 0;
  const entryIndex = firstBarAfterEntry < 0 ? data.length - 1 : firstBarAfterEntry;
  const positionStartX = entryIndex > 0
    ? (x(entryIndex - 1) + x(entryIndex)) / 2
    : MARGIN.left;
  const priceTicks = Array.from({ length: 5 }, (_, index) => minPrice + ((maxPrice - minPrice) * index) / 4);
  const timeTicks = Array.from(new Set([0, Math.floor((data.length - 1) / 2), data.length - 1]));
  const referenceLabels = {};
  let previousLabelY = MARGIN.top - 24;
  [['entry', entryPrice], ['stop', stop], ['target', target]].filter(([, value]) => finite(value)).sort((a, b) => yPrice(a[1]) - yPrice(b[1])).forEach(([key, value]) => {
    referenceLabels[key] = Math.max(previousLabelY + 24, Math.min(PRICE_BOTTOM - 12, yPrice(value)));
    previousLabelY = referenceLabels[key];
  });
  const protectiveOrders = position?.protection_guaranteed === true;
  const timeLabelY = showModelPane ? REGIME_BOTTOM + 28 : PRICE_BOTTOM + 30;

  return (
    <section className={`model-tape ${live && settings.type === "line" ? "is-line" : ""} ${live ? "is-interactive" : ""} ${settings.grid ? "" : "hide-grid"}`} aria-labelledby={`model-tape-${ticker}`}>
      {!live && <div className="model-tape__header">
        <div>
          <h3 id={`model-tape-${ticker}`} className="label">
            {live
              ? marketOnly
                ? "Live market tape"
                : modelOverlayActive
                  ? "Live model decision tape"
                  : "Live position reference tape"
              : "Offline OOS forecast tape"} · {ticker}
          </h3>
          <p className="model-tape__reading" aria-live={live ? "polite" : "off"}>
            {marketOnly ? (
              <strong className="model-tape__signal">MARKET DATA ONLY</strong>
            ) : !modelOverlayActive ? (
              <strong className="model-tape__signal">MODEL TIMEFRAME REFERENCES ONLY</strong>
            ) : (
              <>
                <strong className={`model-tape__signal is-${String(active.signal || "hold").toLowerCase()}`}>
                  {active.signal || "HOLD"}
                </strong>
                <span>ŷ {finite(yhat) ? formatPct(yhat * 100, 2) : "warming"}</span>
                <span>HMM {active.hmm_label || active.state_label || "warming"}</span>
                <span>ATR {finite(active.atr) ? formatUsd(active.atr) : "—"}</span>
              </>
            )}
            {active.complete === false && <span className="is-forming-reading">CURRENT BAR FORMING</span>}
          </p>
        </div>
        <div className="model-tape__legend" aria-label="Chart legend">
          <span className="is-price">Observed OHLC</span>
          {modelOverlayActive && <span className="is-predicted">Model forecast bar</span>}
          {finite(entryPrice) && <span className="is-entry">Position entry</span>}
          {finite(target) && <span className="is-target">Model target</span>}
          {finite(stop) && <span className="is-stop">Model stop</span>}
          {modelOverlayActive && <span className="is-hmm">HMM window / state</span>}
          <button type="button" className="model-tape__jump label" onClick={jumpToLatest}>Jump to latest</button>
        </div>
      </div>}

      {live && <div className="live-chart__controls" role="toolbar" aria-label="Chart controls">
        {toolbar}
        <div className="live-chart__navigation" aria-label="Chart navigation">
        <button onClick={() => pan(-Math.max(1, Math.round(count / 4)))}>Earlier</button>
        <button onClick={() => zoom(1.3)}>Zoom out</button><button onClick={() => zoom(0.75)}>Zoom in</button>
        <button onClick={() => { setCount(100); setManualScale(1); jumpToLatest(); }}>Reset view</button>
        <button aria-pressed={following} onClick={jumpToLatest}>{following ? 'Following latest' : 'Follow latest'}</button>
        <span>{window.start === 0 ? 'Start of loaded history' : `${window.start} earlier bars`} · {allData.length} loaded</span>
      </div></div>}
      <div className="model-tape__canvas" ref={canvasRef}>
        {live && (
          <div className="model-tape__plot-header" aria-live="polite">
            <span className="model-tape__plot-title label">
              {marketOnly ? (marketUpdateMode === "historical_snapshot" ? "Historical market tape" : "Live market tape") : modelOverlayActive ? "Live model decision tape" : "Live position reference tape"} · {ticker}
            </span>
            <span className={`model-tape__plot-signal is-${String(active.signal || "hold").toLowerCase()}`}>
              {marketOnly ? "MARKET DATA ONLY" : !modelOverlayActive ? "MODEL TIMEFRAME REFERENCES ONLY" : `${active.signal || "HOLD"} · ŷ ${finite(yhat) ? formatPct(yhat * 100, 2) : "warming"} · HMM ${active.hmm_label || active.state_label || "warming"} · ATR ${finite(active.atr) ? formatUsd(active.atr) : "—"}`}
            </span>

          </div>
        )}
        <svg
          viewBox={`0 0 ${chartWidth} ${chartHeight}`}
          role="img"
          aria-label={`${ticker} candlestick chart with ${marketUpdateMode === "historical_snapshot" ? "historical snapshot" : "current market"} bars, faded model forecast bars, signal markers, model stop and target references, HMM training window, state bands, and regime probabilities`}
          tabIndex={live ? 0 : undefined}
          onKeyDown={event => {
            if (!live) return;
            if (['ArrowLeft', 'ArrowRight', '+', '-', 'Home', 'End'].includes(event.key)) event.preventDefault();
            if (event.key === 'ArrowLeft') pan(-1);
            if (event.key === 'ArrowRight') pan(1);
            if (event.key === '+') zoom(0.75);
            if (event.key === '-') zoom(1.3);
            if (event.key === 'End') jumpToLatest();
            if (event.key === 'Home') { setEndTime(Date.parse(allData[Math.min(count, allData.length) - 1]?.ts)); setHovered(null); }
          }}
          onWheel={event => {
            if (!live) return;
            const rect = event.currentTarget.getBoundingClientRect();
            zoom(event.deltaY > 0 ? 1.15 : 0.85, (event.clientX - rect.left) / rect.width);
          }}
          onPointerDown={event => {
            if (!live) return;
            const rect = event.currentTarget.getBoundingClientRect();
            dragRef.current = { x: event.clientX, y: event.clientY, end: window.end, scale: manualScale, axis: (event.clientX - rect.left) / rect.width > (chartWidth - rightMargin) / chartWidth };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={event => {
            if (!live) return;
            const rect = event.currentTarget.getBoundingClientRect();
            const px = (event.clientX - rect.left) / rect.width * chartWidth;
            const py = (event.clientY - rect.top) / rect.height * chartHeight;
            setCursorY(Math.max(MARGIN.top, Math.min(PRICE_BOTTOM, py)));
            setHovered(Math.max(0, Math.min(data.length - 1, Math.round((px - MARGIN.left) / plotWidth * horizontalSlots))));
            if (dragRef.current) {
              const drag = dragRef.current;
              if (drag.axis) { setManualScale(Math.max(0.1, Math.min(10, drag.scale * Math.exp((event.clientY - drag.y) / 200)))); return; }
              const shift = Math.round((drag.x - event.clientX) / rect.width * count);
              const end = Math.max(Math.min(count, allData.length), Math.min(allData.length, drag.end + shift));
              setEndTime(Date.parse(allData[end - 1]?.ts)); setHovered(null);
            }
          }}
          onPointerUp={() => { dragRef.current = null; }}
          onPointerCancel={() => { dragRef.current = null; }}
          onDoubleClick={() => setManualScale(1)}
          onMouseLeave={() => { setHovered(null); setCursorY(null); }}
        >
          <rect className="model-tape__plot-bg" x={MARGIN.left} y={MARGIN.top} width={plotWidth} height={PRICE_BOTTOM - MARGIN.top} />
          {modelOverlayActive && (!live || settings.context) && data.map((point, index) => {
            const nextX = index === data.length - 1 ? chartWidth - rightMargin : (x(index) + x(index + 1)) / 2;
            const prevX = index === 0 ? MARGIN.left : (x(index - 1) + x(index)) / 2;
            return (
              <rect
                key={`state-${point.ts}-${index}`}
                className={`model-tape__state-band ${stateClass(point)}`}
                x={prevX}
                y={MARGIN.top}
                width={Math.max(1, nextX - prevX)}
                height={PRICE_BOTTOM - MARGIN.top}
              />
            );
          })}

          {modelOverlayActive && (!live || settings.context) && hmmWindow && (
            <g className="model-tape__hmm-window">
              <rect
                x={hmmBoxStart}
                y={MARGIN.top + 2}
                width={Math.max(1, observedEnd - hmmBoxStart)}
                height={PRICE_BOTTOM - MARGIN.top - 4}
              />
              <text x={hmmBoxStart + 8} y={MARGIN.top + 16}>
                HMM FIT CONTEXT · {hmmVisible}/{hmmWindow} VISIBLE
              </text>
            </g>
          )}

          {position && (!live || settings.context) && finite(stop) && finite(target) && (
            <g className="model-tape__position-band">
              <rect
                x={positionStartX}
                y={Math.min(yPrice(stop), yPrice(target))}
                width={Math.max(1, observedEnd - positionStartX)}
                height={Math.max(1, Math.abs(yPrice(stop) - yPrice(target)))}
              />
              <text
                x={observedEnd - 8}
                y={Math.min(yPrice(stop), yPrice(target)) + 16}
                textAnchor="end"
              >
                OPEN {position.side || "POSITION"} · MODEL RISK ENVELOPE
              </text>
            </g>
          )}

          {priceTicks.map((tick) => (
            <g key={tick}>
              <line className="model-tape__grid" x1={MARGIN.left} x2={chartWidth - rightMargin} y1={yPrice(tick)} y2={yPrice(tick)} />
              <text className="model-tape__axis" x={chartWidth - rightMargin + 8} y={yPrice(tick) + 4}>{formatUsd(tick)}</text>
            </g>
          ))}

          {modelOverlayActive && forecasts.map((forecast, index) => {
            if (!forecast) return null;
            const horizon = Math.max(1, Number(forecast.horizon_bars) || 1);
            const forecastX = x(index + horizon);
            const rising = forecast.close >= forecast.open;
            return (
              <g key={`forecast-${data[index].ts}-${index}`} className={`model-tape__forecast ${live ? "is-projection" : ""}`}>
                <line
                  className={`model-tape__forecast-wick ${rising ? "is-up" : "is-down"}`}
                  x1={forecastX}
                  x2={forecastX}
                  y1={yPrice(forecast.high)}
                  y2={yPrice(forecast.low)}
                />
                <rect
                  className={`model-tape__forecast-candle ${rising ? "is-up" : "is-down"}`}
                  x={forecastX - candleWidth * 0.7}
                  y={Math.min(yPrice(forecast.open), yPrice(forecast.close))}
                  width={candleWidth * 1.4}
                  height={Math.max(2, Math.abs(yPrice(forecast.open) - yPrice(forecast.close)))}
                />
              </g>
            );
          })}

          {live && settings.type === 'line' && <path className="live-chart__price-line" d={linePath(data, x, yPrice, 'close')} />}
          {data.map((point, index) => {
            const rising = point.close >= point.open;
            const markerY = rising ? yPrice(point.low) + 13 : yPrice(point.high) - 13;
            const signal = String(point.signal || "").toUpperCase();
            const showMarker = ["BUY", "SELL", "EXIT"].includes(signal) && (index === 0 || data[index - 1]?.signal !== signal);
            return (
              <g
                key={`${point.ts}-${index}`}
                data-session-date={point.session_date || undefined}
                data-close={point.close}
                onMouseEnter={() => setHovered(index)}
              >
                <rect className="model-tape__hit" x={x(index) - Math.max(5, candleWidth)} y={MARGIN.top} width={Math.max(10, candleWidth * 2)} height={REGIME_BOTTOM - MARGIN.top} />
                <line className={`model-tape__wick ${rising ? "is-up" : "is-down"} ${point.complete === false ? "is-forming" : ""}`} x1={x(index)} x2={x(index)} y1={yPrice((point.high ?? point.close))} y2={yPrice((point.low ?? point.close))} />
                <rect
                  className={`model-tape__candle ${rising ? "is-up" : "is-down"} ${point.complete === false ? "is-forming" : ""}`}
                  x={x(index) - candleWidth / 2}
                  y={Math.min(yPrice(point.open ?? point.close), yPrice(point.close))}
                  width={candleWidth}
                  height={Math.max(1.5, Math.abs(yPrice(point.open ?? point.close) - yPrice(point.close)))}
                />
                {showMarker && modelOverlayActive && (!live || settings.signals) && (
                  <g className={`model-tape__marker is-${signal.toLowerCase()}`}>
                    <path d={signal === "BUY" ? `M${x(index)},${markerY - 7}l-6,10h12z` : `M${x(index)},${markerY + 7}l-6,-10h12z`} />
                    <text x={x(index) + 9} y={markerY + 4}>{signal}</text>
                  </g>
                )}
              </g>
            );
          })}

          {finite(entryPrice) && (
            <g className="model-tape__reference is-entry">
              <line x1={positionStartX} x2={chartWidth - rightMargin} y1={yPrice(entryPrice)} y2={yPrice(entryPrice)} />
              <rect x={chartWidth - rightMargin + 4} y={referenceLabels.entry - 11} width={136} height={22} />
              <text x={chartWidth - rightMargin + 10} y={referenceLabels.entry + 4}>ENTRY {formatUsd(entryPrice)}</text>
            </g>
          )}

          {finite(stop) && (
            <g className="model-tape__reference is-stop">
              <line x1={position ? positionStartX : MARGIN.left} x2={chartWidth - rightMargin} y1={yPrice(stop)} y2={yPrice(stop)} />
              <rect x={chartWidth - rightMargin + 4} y={referenceLabels.stop - 11} width={136} height={22} />
              <text x={chartWidth - rightMargin + 10} y={referenceLabels.stop + 4}>MODEL STOP {formatUsd(stop)}</text>
            </g>
          )}
          {finite(target) && (
            <g className="model-tape__reference is-target">
              <line x1={position ? positionStartX : MARGIN.left} x2={chartWidth - rightMargin} y1={yPrice(target)} y2={yPrice(target)} />
              <rect x={chartWidth - rightMargin + 4} y={referenceLabels.target - 11} width={136} height={22} />
              <text x={chartWidth - rightMargin + 10} y={referenceLabels.target + 4}>MODEL TARGET {formatUsd(target)}</text>
            </g>
          )}

          {showModelPane && (
            <>
              <text className="model-tape__pane-label" x={MARGIN.left} y={REGIME_TOP - 10}>
                {modelOverlayActive ? "HMM / TRANSITION PROBABILITY" : "MODEL PANE · SELECT THE STRATEGY BAR PERIOD FOR OVERLAYS"}
              </text>
              {[0, 0.5, 1].map((tick) => (
                <g key={`prob-${tick}`}>
                  <line className="model-tape__grid" x1={MARGIN.left} x2={chartWidth - rightMargin} y1={yProb(tick)} y2={yProb(tick)} />
                  <text className="model-tape__axis" x={chartWidth - rightMargin + 8} y={yProb(tick) + 4}>{tick.toFixed(1)}</text>
                </g>
              ))}
              {modelOverlayActive && (
                <>
                  <path className="model-tape__probability is-bull" d={linePath(data, x, yProb, "p_bull")} />
                  <path className="model-tape__probability is-side" d={linePath(data, x, yProb, "p_side")} />
                  <path className="model-tape__probability is-bear" d={linePath(data, x, yProb, "p_bear")} />
                </>
              )}
            </>
          )}

          {hovered != null && (!live || settings.crosshair) && <line className="model-tape__crosshair" x1={x(hovered)} x2={x(hovered)} y1={MARGIN.top} y2={showModelPane ? REGIME_BOTTOM : PRICE_BOTTOM} />}
          {live && settings.crosshair && hovered != null && cursorY != null && <line className="model-tape__crosshair" x1={MARGIN.left} x2={chartWidth - rightMargin} y1={cursorY} y2={cursorY} />}
          {live && <text className="model-tape__axis" x={MARGIN.left + 4} y={MARGIN.top + 14}>LAST {formatUsd(latest.close)}</text>}
          {live && <line className="live-chart__last-price" x1={MARGIN.left} x2={chartWidth - rightMargin} y1={yPrice(latest.close)} y2={yPrice(latest.close)} />}
          {timeTicks.map((index) => (
            <text key={`time-${index}`} className="model-tape__axis" textAnchor={index === 0 ? "start" : index === data.length - 1 ? "end" : "middle"} x={x(index)} y={timeLabelY}>
              {live ? new Date(data[index]?.ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...(settings.timezone !== "local" ? { timeZone: settings.timezone } : {}) }) : dateLabel(data[index]?.ts, assetClass, data[index]?.session_date)}
            </text>
          ))}
        </svg>

        {hovered != null && (
          <div className="model-tape__tooltip" aria-hidden="true">
            <span>{dateLabel(active.ts, assetClass, active.session_date)}</span>
            <strong>O {formatUsd(active.open)} · H {formatUsd(active.high)} · L {formatUsd(active.low)} · C {formatUsd(active.close)}</strong>
            {modelOverlayActive && <span>Pred {finite(active.predicted_price) ? formatUsd(active.predicted_price) : "—"}</span>}
            <span>{active.complete === false ? "Forming bar" : "Completed bar"}</span>
            {modelOverlayActive && <span>B/S/B {formatNum(active.p_bull, 2)} / {formatNum(active.p_side, 2)} / {formatNum(active.p_bear, 2)}</span>}
          </div>
        )}
      </div>

      {live && <details className="live-chart__readout"><summary>Inspect bar · {new Date(active.ts).toLocaleString()} · C {formatUsd(active.close)}</summary><p>O {formatUsd(active.open)} · H {formatUsd(active.high)} · L {formatUsd(active.low)} · C {formatUsd(active.close)} · {active.complete === false ? 'Forming' : 'Completed'} bar</p><label>Inspect loaded bar<input aria-label="Inspect loaded bar" type="range" min="0" max={data.length - 1} value={hovered ?? data.length - 1} onChange={e => setHovered(Number(e.target.value))} /></label><p>{position?.protection_guaranteed === true ? 'Broker OCA protection acknowledged' : 'Stop / target are model references; broker protection is not guaranteed.'}</p>{settings.forecasts && <p>Model forecast · half-ATR display envelope; not a confidence interval.</p>}</details>}
      {!live && (
        <div className="model-tape__footer">
          <span>Market source: {marketSource}</span>
          {modelOverlayActive && <span>HMM context: {hmmWindow ? `${hmmLoaded}/${hmmWindow} bars${boundaryIndex == null ? " · start before price view" : ""}` : `${hmmLoaded} bars`}</span>}
          {modelOverlayActive && <span>Threshold: {finite(threshold) ? formatPct(threshold * 100, 2) : "—"}</span>}
          <span>Bar: {pointTimestampLabel(active, assetClass)}</span>
          {position && <span>Position: {position.side} · {formatNum(position.qty, assetClass === "equity" ? 0 : 6)} units</span>}
          {!position && referencePoint && <span>Last signal reference: {referencePoint.signal} · {new Date(referencePoint.ts).toLocaleString()}</span>}
          {modelOverlayActive && <span>Forecast bars: predicted close · half-ATR display envelope</span>}
          {!marketOnly && !modelOverlayActive && (
            <strong className="is-reference-only">
              Bar overlays use the strategy period ({modelBarHours === 24 ? "1 day" : `${modelBarHours || "—"} hours`})
            </strong>
          )}
          {(finite(stop) || finite(target)) && (
            <strong className={protectiveOrders ? "is-protected" : "is-reference-only"}>
              {protectiveOrders ? "Broker OCA stop / target acknowledged for this position" : "Stop / target are model references — protection is not broker-guaranteed"}
            </strong>
          )}
          {marketOnly && <strong className="is-reference-only">Model is not subscribed to this ticker</strong>}
          {mock && <strong className="is-demo">Demonstration data</strong>}
          <strong className="is-reference-only">Offline walk-forward reconstruction · not the execution ledger</strong>
        </div>
      )}
      {modelOverlayActive && (!live || settings.context) && hmmContext.length > 0 && (
        <div
          className="model-tape__window-overview"
          aria-label={`HMM training context showing ${hmmLoaded} of ${hmmWindow || hmmLoaded} bars`}
          title={`HMM training context · ${hmmLoaded}/${hmmWindow || hmmLoaded} bars loaded`}
        >
          {hmmContext.map((point, index) => (
            <span key={`${point.ts}-${index}`} className={stateClass(point)} />
          ))}
        </div>
      )}
    </section>
  );
}
