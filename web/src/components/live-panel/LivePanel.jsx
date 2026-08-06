import { useEffect, useState } from "react";

import { api } from "../../lib/api.js";
import { formatNum, formatPct, formatUsd } from "../../lib/format.js";
import { useInterval } from "../../hooks/useInterval.js";
import "./live-panel.css";

export default function LivePanel({ mode = "paper" }) {
  const [positions, setPositions] = useState(null);
  const [risk, setRisk] = useState(null);
  const [feedStatus, setFeedStatus] = useState("loading");
  const [feedError, setFeedError] = useState(null);

  async function refresh() {
    try {
      const [nextPositions, nextRisk] = await Promise.all([
        api.getLivePositions(),
        api.getLiveRisk(),
      ]);
      setPositions(nextPositions);
      setRisk(nextRisk);
      setFeedStatus("ready");
      setFeedError(null);
    } catch (error) {
      setFeedStatus(positions || risk ? "stale" : "error");
      setFeedError(`Live position and risk feed unavailable: ${error.message}`);
    }
  }

  useEffect(() => {
    refresh();
  }, []);
  useInterval(refresh, 10000);
  const statusUnknown = feedStatus !== "ready";

  return (
    <section className="live-panel" aria-labelledby="live-risk-title">
      <div className="live-panel__header">
        <h2 id="live-risk-title" className="label">
          {mode === "live" ? "Live Positions & Risk" : "Paper Positions & Risk"}
        </h2>
        <div className="live-panel__feed-status" role="status" aria-live="polite">
          {feedStatus === "loading" && (
            <span className="live-panel__status-tag label">CONNECTING…</span>
          )}
          {feedStatus === "error" && (
            <span className="live-panel__status-tag is-error label">STATUS UNKNOWN — FEED OFFLINE</span>
          )}
          {feedStatus === "stale" && (
            <span className="live-panel__status-tag is-error label">DATA STALE — CONNECTION LOST</span>
          )}
          {feedStatus === "ready" && positions?.mock && (
            <span className="live-panel__mock-tag label">SIMULATED FEED — NO LIVE CONNECTION</span>
          )}
        </div>
      </div>

      {feedError && <div className="live-panel__feed-error">{feedError}</div>}

      <div className="live-panel__body">
        <div className="live-panel__positions">
          <table className="live-panel__table">
            <caption className="sr-only">Current positions from the live or simulated feed</caption>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Quantity</th>
                <th>Price</th>
                <th>Change</th>
                <th>MKT Value</th>
                <th>Day Change</th>
                <th>Gain/Loss</th>
              </tr>
            </thead>
            <tbody>
              {(positions?.positions || []).map((p) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td>
                  <td className="num">{formatNum(p.qty, 4)}</td>
                  <td className="num">{formatUsd(p.mark_price)}</td>
                  <td className={`num ${(p.change_pct ?? 0) >= 0 ? "is-positive" : "is-negative"}`}>
                    {formatPct(p.change_pct ?? (p.avg_price ? ((p.mark_price - p.avg_price) / p.avg_price) * 100 : 0))}
                  </td>
                  <td className="num">{formatUsd(p.market_value ?? p.notional ?? p.qty * p.mark_price)}</td>
                  <td className={`num ${(p.day_change ?? p.unrealized_pnl ?? 0) >= 0 ? "is-positive" : "is-negative"}`}>
                    {formatUsd(p.day_change ?? p.unrealized_pnl ?? 0)}
                  </td>
                  <td className={`num ${(p.gain_loss_total ?? p.unrealized_pnl ?? 0) >= 0 ? "is-positive" : "is-negative"}`}>
                    {formatUsd(p.gain_loss_total ?? p.unrealized_pnl ?? 0)}
                  </td>
                </tr>
              ))}
              {statusUnknown && !positions?.positions?.length && (
                <tr>
                  <td colSpan={7} className="live-panel__empty is-error">
                    Position status unknown
                  </td>
                </tr>
              )}
              {!statusUnknown && !positions?.positions?.length && (
                <tr>
                  <td colSpan={7} className="live-panel__empty">
                    No positions
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="live-panel__risk">
          <div className="live-panel__risk-row">
            <span className="label">Equity</span>
            <span className="num display">{risk ? formatUsd(risk.equity) : "UNKNOWN"}</span>
          </div>
          <div className="live-panel__risk-row">
            <span className="label">Daily PnL</span>
            <span
              className={`num ${
                risk ? (risk.daily_pnl_pct >= 0 ? "is-positive" : "is-negative") : ""
              }`}
            >
              {risk ? formatPct(risk.daily_pnl_pct) : "UNKNOWN"}
            </span>
          </div>
          <div className="live-panel__risk-row">
            <span className="label">Drawdown</span>
            <span className="num">{risk ? formatPct(risk.drawdown_pct) : "UNKNOWN"}</span>
          </div>
          <div className="live-panel__risk-row">
            <span className="label">Gross Leverage</span>
            <span className="num">
              {risk ? `${formatNum(risk.gross_leverage, 2)}x` : "UNKNOWN"}
            </span>
          </div>
          <div className="live-panel__rails">
            <div className="label">Risk Rails (fixed)</div>
            {risk?.rails && (
              <ul>
                <li>Target / cap: {risk.rails.risk_budget_pct}% / {risk.rails.hard_cap_pct}%</li>
                <li>Leverage max: {risk.rails.leverage_max}x</li>
                <li>Daily loss limit: {risk.rails.daily_loss_limit_pct}%</li>
                <li>Drawdown warn: {risk.rails.drawdown_warn_pct}%</li>
                <li>Kill-switch: {risk.rails.kill_switch_pct}%</li>
              </ul>
            )}
            <div
              className={`live-panel__kill-switch ${
                statusUnknown ? "is-unknown" : risk?.kill_switch_engaged ? "is-engaged" : ""
              }`}
            >
              KILL-SWITCH{" "}
              {statusUnknown ? "STATUS UNKNOWN" : risk?.kill_switch_engaged ? "ENGAGED" : "CLEAR"}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
