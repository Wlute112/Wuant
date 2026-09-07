import { formatTime, formatUsd } from "../../lib/format.js";
import { jobDisplayName, jobStatusLabel } from "../../lib/jobs.js";
import "./run-list.css";

export default function RunList({
  runs,
  activeRunId,
  compareRunId,
  onSelect,
  onCompare,
  onDelete,
  activeJobs = [],
  selectedJobId = null,
  onSelectJob,
  loadStatus = "loading",
}) {
  return (
    <section className="run-list" aria-labelledby="runs-title">
      <h2 id="runs-title" className="label run-list__heading">
        Runs ({runs.length + activeJobs.length})
      </h2>
      {loadStatus === "loading" && <div className="run-list__empty">Loading runs…</div>}
      {loadStatus === "error" && (
        <div className="run-list__empty is-error">Run list unavailable — API status unknown</div>
      )}
      {loadStatus === "ready" && runs.length === 0 && activeJobs.length === 0 && (
        <div className="run-list__empty">No runs yet — trigger a backtest or Optuna sweep above.</div>
      )}
      <div className="run-list__rows">
        {activeJobs.map((job) => {
          const displayName = jobDisplayName(job);
          return (
            <div key={job.id} className={`run-list__row is-job ${job.id === selectedJobId ? "is-active" : ""}`}>
              <button
                type="button"
                className="run-list__select"
                aria-pressed={job.id === selectedJobId}
                onClick={() => onSelectJob?.(job.id)}
              >
                <span className="run-list__kind run-list__live-kind label"><i aria-hidden="true" />live</span>
                <span className="run-list__identity">
                  <span className="run-list__name" title={displayName}>{displayName}</span>
                  <span className="run-list__id" title={job.id}>{job.kind === "optimize" ? "OPTUNA SWEEP" : "HISTORICAL REPLAY"}</span>
                </span>
                <span className="label">{formatTime(job.started_at)}</span>
                <span className="num run-list__job-status">{jobStatusLabel(job.status)}</span>
              </button>
            </div>
          );
        })}
        {runs.map((run) => {
          const netProfit = run.metrics?.net_profit_usd;
          const displayName = run.name?.trim() || run.run_id;
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
                <span className="run-list__identity">
                  <span className="run-list__name" title={displayName}>{displayName}</span>
                  {run.name && <span className="run-list__id" title={run.run_id}>{run.run_id}</span>}
                </span>
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
                aria-label={`${run.run_id === compareRunId ? "Remove" : "Add"} ${displayName} ${
                  run.run_id === compareRunId ? "from" : "to"
                } comparison`}
                onClick={() => onCompare(run.run_id === compareRunId ? null : run.run_id)}
                disabled={run.run_id === activeRunId}
                title={run.run_id === activeRunId ? "The open run is already the comparison baseline" : "Overlay this run on the open run's performance charts"}
              >
                {run.run_id === compareRunId ? "comparing" : "compare"}
              </button>
              <details className="run-list__menu">
                <summary className="label" aria-label={`More actions for ${displayName}`}>•••</summary>
                <div className="run-list__menu-popover">
                  <button
                    className="run-list__delete label"
                    type="button"
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete run ${displayName} (${run.run_id})? This removes its artifact and job files and cannot be undone.`,
                        )
                      ) {
                        onDelete(run.run_id);
                      }
                    }}
                  >
                    Delete run…
                  </button>
                </div>
              </details>
            </div>
          );
        })}
      </div>
    </section>
  );
}
