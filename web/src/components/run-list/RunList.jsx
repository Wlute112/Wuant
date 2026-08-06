import { formatTime, formatUsd } from "../../lib/format.js";
import "./run-list.css";

export default function RunList({
  runs,
  activeRunId,
  compareRunId,
  onSelect,
  onCompare,
  onDelete,
  loadStatus = "loading",
}) {
  return (
    <section className="run-list" aria-labelledby="runs-title">
      <h2 id="runs-title" className="label run-list__heading">
        Runs ({runs.length})
      </h2>
      {loadStatus === "loading" && <div className="run-list__empty">Loading runs…</div>}
      {loadStatus === "error" && (
        <div className="run-list__empty is-error">Run list unavailable — API status unknown</div>
      )}
      {loadStatus === "ready" && runs.length === 0 && (
        <div className="run-list__empty">No runs yet — trigger a backtest or Optuna sweep above.</div>
      )}
      <div className="run-list__rows">
        {runs.map((run) => {
          const netProfit = run.metrics?.net_profit_usd;
          return (
            <div
              key={run.run_id}
              className={`run-list__row ${run.run_id === activeRunId ? "is-active" : ""}`}
            >
              <button
                type="button"
                className="run-list__select"
                aria-pressed={run.run_id === activeRunId}
                onClick={() => onSelect(run.run_id)}
              >
                <span className="run-list__kind label">{run.kind}</span>
                <span className="run-list__id">{run.run_id}</span>
                <span className="label">{formatTime(run.finished_at)}</span>
                <span
                  className={`num run-list__pnl ${
                    netProfit > 0 ? "is-positive" : netProfit < 0 ? "is-negative" : ""
                  }`}
                >
                  {netProfit != null ? formatUsd(netProfit) : "—"}
                </span>
              </button>
              <button
                className={`run-list__compare label ${run.run_id === compareRunId ? "is-active" : ""}`}
                type="button"
                aria-pressed={run.run_id === compareRunId}
                aria-label={`${run.run_id === compareRunId ? "Remove" : "Add"} ${run.run_id} ${
                  run.run_id === compareRunId ? "from" : "to"
                } comparison`}
                onClick={() => onCompare(run.run_id === compareRunId ? null : run.run_id)}
                disabled={run.run_id === activeRunId}
              >
                compare
              </button>
              <button
                className="run-list__delete label"
                type="button"
                onClick={() => {
                  if (
                    window.confirm(
                      `Delete run ${run.run_id}? This removes its artifact and job files and cannot be undone.`,
                    )
                  ) {
                    onDelete(run.run_id);
                  }
                }}
                title="Delete this run"
              >
                delete
              </button>
            </div>
          );
        })}
      </div>
    </section>
  );
}
