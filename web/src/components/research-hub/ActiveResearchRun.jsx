import { useCallback, useEffect, useMemo, useState } from "react";

import DataReport from "./DataReport.jsx";
import ChannelStrip from "../channel-strip/ChannelStrip.jsx";
import { useInterval } from "../../hooks/useInterval.js";
import { api } from "../../lib/api.js";
import { formatNum, formatPct, formatUsd } from "../../lib/format.js";
import { isJobActive, jobDisplayName, jobStatusLabel } from "../../lib/jobs.js";
import "./active-research-run.css";

const PHASES = {
  backtest: [
    ["loading", "Load history"],
    ["replay", "Replay market"],
    ["reporting", "Compute evidence"],
    ["saving", "Save result"],
  ],
  optimize: [
    ["preparing", "Build folds"],
    ["search", "Search candidates"],
    ["holdout", "Outer holdout"],
    ["saving", "Save result"],
  ],
};

function durationLabel(startedAt, finishedAt, now) {
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt) || now;
  if (!Number.isFinite(start)) return "—";
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function compactDate(value) {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "Waiting for first bar";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(timestamp);
}

export default function ActiveResearchRun({ job, onJobUpdated }) {
  const [progress, setProgress] = useState({});
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  const [now, setNow] = useState(Date.now());
  const active = isJobActive(job);
  const phases = PHASES[job?.kind] || [];
  const phaseKey = progress.phase === "complete" ? phases.at(-1)?.[0] : progress.phase;
  const phaseIndex = Math.max(0, phases.findIndex(([key]) => key === phaseKey));

  const refresh = useCallback(async () => {
    if (!job?.id) return;
    try {
      const [nextProgress, output] = await Promise.all([
        api.getJobProgress(job.id),
        api.getJobLogs(job.id, 120),
      ]);
      setProgress(nextProgress);
      setLogs(output.lines || []);
      setError(null);
    } catch (requestError) {
      setError(`Live research telemetry is unavailable: ${requestError.message}`);
    }
  }, [job?.id]);

  useEffect(() => { refresh(); }, [refresh, job?.status]);
  useInterval(refresh, active ? 1000 : null);
  useInterval(() => setNow(Date.now()), active ? 1000 : null);

  const backtestSeries = useMemo(
    () => (progress.timeline || []).map((point) => ({ x: Date.parse(point.ts), y: point.equity })),
    [progress.timeline],
  );
  const candidateSeries = useMemo(
    () => (progress.history || []).filter((item) => item.score != null).map((item) => ({ x: item.trial, y: item.score })),
    [progress.history],
  );
  const bestSeries = useMemo(
    () => (progress.history || []).filter((item) => item.best != null).map((item) => ({ x: item.trial, y: item.best })),
    [progress.history],
  );
  const percent = Number.isFinite(progress.percent) ? Math.max(0, Math.min(100, progress.percent)) : null;
  const config = job?.config || {};
  const tickers = progress.tickers || config.tickers || [];

  async function cancel() {
    if (!active || cancelling) return;
    setCancelling(true);
    try {
      const updated = await api.cancelJob(job.id);
      onJobUpdated?.(updated);
    } catch (cancelError) {
      setError(`Could not cancel this run: ${cancelError.message}`);
    } finally {
      setCancelling(false);
    }
  }

  if (job.kind === "data_repair") return (
    <article className="active-research">
      <header className="active-research__header">
        <div><h2>Observed data repair</h2><p role="status">{jobStatusLabel(job.status)} · {progress.phase_label || "Waiting for broker history"}</p></div>
        {active && <button type="button" disabled={cancelling || job.status === "cancelling"} onClick={cancel}>{cancelling ? "Stopping…" : "Cancel data fetch"}</button>}
      </header>
      <p>{config.csv} · {tickers.join(" · ")}</p>
      {(error || job.failure_reason) && <p role="alert">{error || job.failure_reason}</p>}
      {progress.report && <div className="data-preflight"><DataReport report={progress.report} /></div>}
      <details className="active-research__logs"><summary>Broker output and backup location</summary><pre>{logs.length ? logs.join("\n") : "No process output yet."}</pre></details>
    </article>
  );

  if (job.kind.startsWith("campaign_")) return (
    <article className="active-research" aria-live="polite">
      <header className="active-research__header">
        <div><h2>{job.kind.replace("campaign_", "Campaign ")}</h2><p>{jobStatusLabel(job.status)} · {progress.phase_label || "Review process output for stage progress"}</p></div>
        {active && <button type="button" disabled={cancelling || job.status === "cancelling"} onClick={cancel}>{cancelling || job.status === "cancelling" ? "Stopping…" : "Cancel run"}</button>}
      </header>
      <p>Campaign: {config.campaign_id} · Job: {job.id}</p>
      {(error || job.failure_reason) && <p role="alert">{error || job.failure_reason}</p>}
      <details className="active-research__logs"><summary>Stage progress evidence</summary><pre>{JSON.stringify(progress, null, 2)}</pre></details>
      <details className="active-research__logs"><summary>Technical output</summary><pre>{logs.length ? logs.join("\n") : "No process output yet."}</pre></details>
    </article>
  );

  const readings = job.kind === "optimize"
    ? [
        ["Trial", progress.trial_current ? `${progress.trial_current}${progress.trials_target ? ` / ${progress.trials_target}` : ""}` : "—"],
        ["Fold", progress.fold_current != null ? `${progress.fold_current} / ${progress.fold_total || "—"}` : "—"],
        ["Best score", progress.best_score == null ? "—" : formatNum(progress.best_score, 4)],
        ["Candidate", progress.candidate_score == null ? "—" : formatNum(progress.candidate_score, 4)],
        ["Pruned", progress.trials_pruned ?? 0],
        ["Elapsed", durationLabel(job.started_at, job.finished_at, now)],
      ]
    : [
        ["Market time", compactDate(progress.as_of)],
        ["Bars", progress.bars_total ? `${progress.bars_processed || 0} / ${progress.bars_total}` : "—"],
        ["Equity", progress.equity == null ? formatUsd(progress.starting_cash || config.cash) : formatUsd(progress.equity)],
        ["Net P&L", progress.net_pnl == null ? "—" : formatUsd(progress.net_pnl)],
        ["Max drawdown", progress.max_drawdown_pct == null ? "—" : formatPct(progress.max_drawdown_pct, 2)],
        ["Trades", progress.trades ?? 0],
      ];

  return (
    <article className="active-research" aria-live="polite">
      <header className="active-research__header">
        <div>
          <div className="active-research__tags">
            <span className={active ? "is-live" : ""}>{active && <i aria-hidden="true" />}{active ? "LIVE COMPUTE" : jobStatusLabel(job.status).toUpperCase()}</span>
            <span>{job.kind.startsWith("campaign_") ? "VALIDATION CAMPAIGN" : job.kind === "optimize" ? "OPTUNA SWEEP" : "HISTORICAL REPLAY"}</span>
            <span>{String(config.asset_class || "crypto").toUpperCase()}</span>
          </div>
          <h1>{jobDisplayName(job)}</h1>
          <p>{tickers.join(" · ") || "Universe pending"} · {progress.phase_label || jobStatusLabel(job.status)}</p>
        </div>
        {active && (
          <button type="button" className="active-research__cancel" disabled={cancelling || job.status === "cancelling"} onClick={cancel}>
            {cancelling || job.status === "cancelling" ? "Stopping…" : "Cancel run"}
          </button>
        )}
      </header>

      <section className="active-research__process" aria-label="Research process">
        <ol>
          {phases.map(([key, label], index) => (
            <li key={key} className={index < phaseIndex || progress.phase === "complete" ? "is-complete" : index === phaseIndex ? "is-current" : ""} aria-current={index === phaseIndex ? "step" : undefined}>
              <i aria-hidden="true" />
              <span>{label}</span>
            </li>
          ))}
        </ol>
        <div className="active-research__progress-track" aria-label={percent == null ? "Progress is indeterminate" : `${percent}% complete`}>
          <span className={percent == null ? "is-indeterminate" : ""} style={percent == null ? undefined : { width: `${percent}%` }} />
        </div>
      </section>

      {job.failure_reason && <div className="active-research__error" role="alert">{job.failure_reason}</div>}
      {error && <div className="active-research__error" role="alert">{error}</div>}

      <dl className="active-research__readings">
        {readings.map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="num">{value}</dd></div>)}
      </dl>

      <section className="active-research__visual" aria-label="Live research visualization">
        {job.kind === "optimize" ? (
          <ChannelStrip
            label="CANDIDATE SCORE"
            color="var(--color-trace-amber)"
            series={candidateSeries}
            overlaySeries={bestSeries}
            overlayColor="var(--color-text-primary)"
            overlayLabel="incumbent"
            currentValueLabel={progress.best_score == null ? "—" : formatNum(progress.best_score, 4)}
            emptyMessage="The first walk-forward candidate is being evaluated"
            tickFormat={(value) => formatNum(value, 3)}
            height={310}
          />
        ) : (
          <ChannelStrip
            label="HISTORICAL EQUITY"
            color="var(--color-trace-amber)"
            series={backtestSeries}
            currentValueLabel={progress.equity == null ? "—" : formatUsd(progress.equity)}
            emptyMessage="Loading the first historical segment"
            tickFormat={(value) => formatUsd(value)}
            height={310}
          />
        )}
      </section>

      <div className="active-research__context">
        <span><strong>Source</strong>{config.csv || "Default dataset"}</span>
        <span><strong>Capital</strong>{formatUsd(config.cash || progress.starting_cash)}</span>
        <span><strong>Job ID</strong>{job.id}</span>
      </div>

      <details className="active-research__logs">
        <summary>Technical output <span>{logs.length} lines</span></summary>
        <pre>{logs.length ? logs.join("\n") : "No process output yet."}</pre>
      </details>
    </article>
  );
}
