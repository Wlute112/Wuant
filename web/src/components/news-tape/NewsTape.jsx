/*
THESIS: A dense broker-news instrument shows the model's interpretation in place, refusing a detached notification-card feed.
OWN-WORLD: Near-black machined housing, paper rows, hairline rules, tabular mono readings, and semantic up/down ink.
STORY: Scan the newest headline, see which strategy instruments the local model connects, inspect its news-only move estimate, then jump to that chart.
FIRST VIEWPORT: A collapsible bottom-right dock keeps source/time, headline, linked tickers, and move estimates visible without leaving the live console.
FORM: Established Strip Recorder world, extended as a TWS-style rolling tape; no new visual system or route.
*/
import { useEffect, useMemo, useState } from "react";

import { formatTime } from "../../lib/format.js";
import "./news-tape.css";


function formatMove(value) {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(3)}%`;
}

function formatScore(value) {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}`;
}

function driverLabel(value) {
  const [kind, detail] = String(value || "").split(":", 2);
  if (!detail) return kind.toUpperCase();
  return `${kind.toUpperCase()} · ${detail.replaceAll("_", " ").toUpperCase()}`;
}

function analysisLabel(value) {
  return value === "local_llm" ? "LOCAL LLM" : "RULE FALLBACK";
}

function ImpactButton({ impact, onSelectTicker }) {
  const positive = impact.direction === "UP";
  const directionKnown = impact.direction === "UP" || impact.direction === "DOWN";
  const predictedMove = Number(impact.predicted_move_pct);
  const unavailableLabel = impact.factor_eligible ? "FIT N/A" : "OUT OF WINDOW";
  return (
    <button
      type="button"
      className={`news-tape__impact ${directionKnown ? (positive ? "is-positive" : "is-negative") : "is-neutral"}`}
      onClick={() => onSelectTicker?.(impact.symbol)}
      title={`${impact.symbol}: ${impact.drivers.map(driverLabel).join(", ")}; ${directionKnown ? "news-only estimate" : unavailableLabel.toLowerCase()}`}
    >
      <span>{impact.symbol}</span>
      <span className="news-tape__impact-arrow" aria-hidden="true">{directionKnown ? (positive ? "↑" : "↓") : "·"}</span>
      <span className="num">{directionKnown ? formatMove(predictedMove) : unavailableLabel}</span>
      <span className="sr-only">
        {directionKnown
          ? `predicted news-only move ${impact.direction.toLowerCase()} ${Number.isFinite(predictedMove) ? Math.abs(predictedMove).toFixed(3) : "unknown"} percent`
          : `prediction unavailable, ${unavailableLabel.toLowerCase()}`}
        {`; connected via ${impact.drivers.map(driverLabel).join(", ")}`}
      </span>
    </button>
  );
}

function AnalysisDetail({ item }) {
  const labels = [
    ...(item.industries || []).map((entry) => ({ ...entry, kind: "industry" })),
    ...(item.commodities || []).map((entry) => ({ ...entry, kind: "commodity" })),
  ];
  return (
    <div className="news-tape__analysis">
      {(item.analysis_superseded || item.connection_superseded) && (
        <p className="news-tape__provenance">
          {item.connection_superseded
            ? `Factor used an earlier ${analysisLabel(item.factor_analysis_kind)} connection at the completed bar; the latest ${analysisLabel(item.analysis_kind)} no longer links at least one strategy ticker.`
            : `Factor used ${analysisLabel(item.factor_analysis_kind)} at the completed bar; details below show the latest ${analysisLabel(item.analysis_kind)} refinement.`}
        </p>
      )}
      <p>{item.summary || "No additional summary was supplied."}</p>
      <dl className="news-tape__readings">
        <div><dt>Confidence</dt><dd className="num">{Math.round(item.confidence * 100)}%</dd></div>
        <div><dt>Urgency</dt><dd className="num">{item.urgency}/10</dd></div>
        <div><dt>Macro</dt><dd className="num">{formatScore(item.macro_score)}</dd></div>
        <div><dt>Scope</dt><dd>{String(item.scope || "broad").toUpperCase()}</dd></div>
      </dl>
      {labels.length > 0 && (
        <div className="news-tape__taxonomy" aria-label="Connected industries and commodities">
          {labels.slice(0, 10).map((entry) => (
            <span key={`${entry.kind}-${entry.name}`}>
              {entry.kind === "commodity" ? "CMDTY" : "IND"} · {entry.name.replaceAll("_", " ")} <b className="num">{formatScore(entry.score)}</b>
            </span>
          ))}
        </div>
      )}
      {item.url && (
        <a href={item.url} target="_blank" rel="noreferrer">Open source article ↗</a>
      )}
    </div>
  );
}

export default function NewsTape({
  feed,
  status = "loading",
  error = null,
  paused = false,
  active = false,
  factorEnabled = false,
  embedded = false,
  onTogglePause,
  onSelectTicker,
}) {
  const [collapsed, setCollapsed] = useState(
    () => !embedded && typeof window !== "undefined" && window.matchMedia("(max-width: 900px)").matches,
  );
  const [selectedId, setSelectedId] = useState(null);
  const items = feed?.items || [];
  const itemIds = useMemo(() => new Set(items.map((item) => item.id)), [items]);

  useEffect(() => {
    if (selectedId && !itemIds.has(selectedId)) setSelectedId(null);
  }, [itemIds, selectedId]);

  useEffect(() => {
    if (embedded) {
      setCollapsed(false);
      return undefined;
    }
    const media = window.matchMedia("(max-width: 900px)");
    const handleCompact = (event) => {
      if (event.matches) setCollapsed(true);
    };
    media.addEventListener("change", handleCompact);
    return () => media.removeEventListener("change", handleCompact);
  }, [embedded]);

  const localCount = items.filter((item) => item.analysis_kind === "local_llm").length;
  const stateLabel = error
    ? "FEED ERROR"
    : feed?.status === "unavailable"
      ? "DATABASE WAITING"
      : paused
        ? "PAUSED"
        : active
          ? "ROLLING · 2S"
          : "ARCHIVE · SESSION OFF";

  return (
    <aside className={`news-tape ${embedded ? "is-embedded" : ""} ${collapsed ? "is-collapsed" : ""}`} aria-labelledby="news-tape-title">
      <header className="news-tape__header">
        <div className="news-tape__title-group">
          <span className={`news-tape__lamp ${active && !paused && !error ? "is-active" : ""}`} aria-hidden="true" />
          <div>
            <h3 id="news-tape-title">Live news / model impact</h3>
            <span className="label">{stateLabel}</span>
          </div>
        </div>
        <div className="news-tape__controls">
          {!collapsed && (
            <button type="button" onClick={onTogglePause} aria-pressed={paused}>
              {paused ? "Resume" : "Pause"}
            </button>
          )}
          {!embedded && (
            <button
              type="button"
              onClick={() => setCollapsed((value) => !value)}
              aria-expanded={!collapsed}
              aria-controls="news-tape-stream"
            >
              {collapsed ? "Open" : "Hide"}
            </button>
          )}
        </div>
      </header>

      {!collapsed && (
        <>
          <div className="news-tape__meter" aria-label="News feed status">
            <span>{items.length} HEADLINES</span>
            <span>{localCount} LOCAL LLM</span>
            <span>
              {feed?.prediction_basis === "fit_coefficient_not_available_per_article"
                ? "FIT IMPULSE N/A"
                : `${factorEnabled ? "RAW CAP" : "SCENARIO CAP"} ±${(feed?.max_impulse_pct ?? ((feed?.scale ?? 0.001) * 100)).toFixed(2)}%`}
            </span>
            <span>LAST {formatTime(feed?.latest_received_at)}</span>
          </div>

          {error && <div className="news-tape__error" role="alert">{error}</div>}

          <div id="news-tape-stream" className="news-tape__stream grain" role="feed" aria-busy={status === "loading"}>
            {items.map((item, index) => {
              const expanded = selectedId === item.id;
              return (
                <article
                  key={item.id}
                  className={`news-tape__item ${index === 0 ? "is-latest" : ""}`}
                  aria-labelledby={`news-tape-headline-${index}`}
                  aria-posinset={index + 1}
                  aria-setsize={items.length}
                >
                  <div className="news-tape__meta">
                    <time dateTime={item.received_at}>{formatTime(item.received_at)}</time>
                    <span>{item.source_name || item.provider || item.source_kind}</span>
                    <span className={item.analysis_kind === "local_llm" ? "is-llm" : ""}>
                      {item.analysis_superseded ? "LATEST " : ""}{analysisLabel(item.analysis_kind)}
                    </span>
                    {item.analysis_superseded && (
                      <span>USED {analysisLabel(item.factor_analysis_kind)}</span>
                    )}
                    {item.connection_superseded && <span>LATEST UNLINKED</span>}
                    {factorEnabled && item.connected_to_strategy && (
                      <span>{item.factor_eligible ? "IN FACTOR" : "OUT OF WINDOW"}</span>
                    )}
                  </div>
                  <button
                    id={`news-tape-headline-${index}`}
                    type="button"
                    className="news-tape__headline"
                    onClick={() => setSelectedId(expanded ? null : item.id)}
                    aria-expanded={expanded}
                  >
                    <span>{item.title}</span>
                    <span aria-hidden="true">{expanded ? "−" : "+"}</span>
                  </button>
                  <div className="news-tape__impacts" aria-label="LLM-linked instruments and predicted news-only moves">
                    {(item.connections || []).map((impact) => (
                      <ImpactButton key={impact.symbol} impact={impact} onSelectTicker={onSelectTicker} />
                    ))}
                    {!item.connections?.length && <span className="news-tape__unlinked">NO LINKED STRATEGY TICKER</span>}
                  </div>
                  {expanded && <AnalysisDetail item={item} />}
                </article>
              );
            })}
            {!items.length && !error && (
              <div className="news-tape__empty">
                <strong>{feed?.status === "unavailable" ? "NEWS DATABASE NOT AVAILABLE" : "WAITING FOR HEADLINES"}</strong>
                <span>{feed?.status === "unavailable" ? "Start a paper/live node to activate RSS and IBKR collection." : "The next normalized RSS or IBKR story will appear here."}</span>
              </div>
            )}
          </div>
          <footer className="news-tape__footer">
            <span>
              {feed?.prediction_basis === "fit_coefficient_not_available_per_article"
                ? "MOVE = FIT COEFFICIENT · PER-ARTICLE N/A"
                : feed?.prediction_basis === "causal_marginal_factor_contribution"
                  ? "MOVE = CAUSAL MARGINAL NEWS CONTRIBUTION"
                  : "MOVE = UNWEIGHTED ANALYSIS SCENARIO"}
            </span>
            <span>{factorEnabled ? "FACTOR ON" : "FACTOR OFF"}</span>
            <span>NEWS-ONLY · NOT TOTAL Ŷ</span>
          </footer>
        </>
      )}
    </aside>
  );
}
