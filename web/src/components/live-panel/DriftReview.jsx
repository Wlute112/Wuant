import { useEffect, useState } from "react";
import { useInterval } from "../../hooks/useInterval.js";
import { api } from "../../lib/api.js";
import "./drift-review.css";

const DEFAULTS = {
  min_matched: 20,
  max_prediction_mae_bps: 20,
  max_prediction_magnitude_shift_bps: 20,
  max_signal_disagreement_pct: 20,
  max_regime_disagreement_pct: 30,
  max_bull_share_shift_pct: 30,
  max_slippage_bps: 30,
  max_reject_rate_pct: 5,
  max_data_age_minutes: 60,
};
const LIMITS = [
  ["min_matched", "Minimum matched samples", "count"],
  ["max_prediction_mae_bps", "Prediction MAE", "bps"],
  ["max_prediction_magnitude_shift_bps", "Prediction magnitude shift", "bps"],
  ["max_signal_disagreement_pct", "Signal disagreement", "%"],
  ["max_regime_disagreement_pct", "Regime disagreement", "%"],
  ["max_bull_share_shift_pct", "Bull share shift", "%"],
  ["max_slippage_bps", "Fill price vs decision close", "bps"],
  ["max_reject_rate_pct", "Rejected orders", "%"],
  ["max_data_age_minutes", "Telemetry / market age", "min"],
];
const LABELS = {
  prediction_mae_bps: "Prediction MAE",
  prediction_magnitude_shift_bps: "Prediction magnitude shift",
  signal_disagreement_pct: "Signal disagreement",
  regime_disagreement_pct: "Regime disagreement",
  bull_share_shift_pct: "Bull share shift",
  slippage_bps: "Fill price vs decision close",
  reject_rate_pct: "Rejected orders",
  data_age_minutes: "Telemetry age",
  market_data_age_minutes: "Market data age",
};
const GROUPS = [
  ["Across periods", ["prediction_magnitude_shift_bps", "bull_share_shift_pct"]],
  ["Execution and freshness", ["slippage_bps", "reject_rate_pct", "market_data_age_minutes", "data_age_minutes"]],
  ["Exact bar overlap", ["prediction_mae_bps", "signal_disagreement_pct", "regime_disagreement_pct"]],
];
const INITIAL_CONFIG = { baseline_run_id: "", ...DEFAULTS };

function alertCount(metrics = {}) {
  return Object.values(metrics).filter((metric) => metric.status === "alert").length;
}

function MetricRow({ name, metric }) {
  return (
    <div className={`drift-review__metric is-${metric.status}`}>
      <dt>{LABELS[name] || name}<small>{metric.count} samples · {metric.status.replaceAll("_", " ")}</small></dt>
      <dd>{metric.value == null ? "—" : `${metric.value} ${metric.unit}`}
        <small>limit {metric.threshold} {metric.unit}{metric.uncertainty_95_pct ? ` · 95% interval ${metric.uncertainty_95_pct.join("–")}%` : ""}</small>
      </dd>
    </div>
  );
}

export default function DriftReview({ jobId, assetClass, isDemo }) {
  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState(null);
  const [config, setConfig] = useState(INITIAL_CONFIG);
  const [review, setReview] = useState(null);
  const [reviewError, setReviewError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");

  useEffect(() => {
    let current = true;
    setRuns([]);
    setRunsLoading(true);
    setRunsError(null);
    api.listRuns().then((items) => {
      if (current) setRuns(items.filter((run) => run.asset_class === assetClass));
    }).catch((err) => { if (current) setRunsError(`Baseline runs unavailable: ${err.message}`); })
      .finally(() => { if (current) setRunsLoading(false); });
    return () => { current = false; };
  }, [assetClass]);

  async function refresh() {
    if (!jobId || isDemo) return;
    try {
      const next = await api.getDriftReview(jobId);
      setReview(next);
      setConfig((current) => next.config && !current.baseline_run_id ? { ...current, ...next.config } : current);
      setReviewError(null);
    } catch (err) {
      setReviewError(`Review unavailable: ${err.message}. Existing readings may be stale.`);
    }
  }
  useEffect(() => {
    setReview(null);
    setConfig(INITIAL_CONFIG);
    setSaveMessage("");
    refresh();
  }, [jobId, isDemo]);
  useInterval(refresh, 5000);

  async function save(event) {
    event.preventDefault();
    setSaving(true);
    setSaveMessage("");
    try {
      await api.saveDriftConfig(jobId, config);
      await refresh();
      setSaveMessage("Review limits saved.");
    } catch (err) {
      setReviewError(`Could not save review limits: ${err.message}`);
    } finally {
      setSaving(false);
    }
  }

  if (isDemo || !jobId) return <p className="drift-review__empty">Start an execution session to compare observed decisions with a saved research run. Demonstration data cannot create drift evidence.</p>;

  const metrics = review?.metrics || {};
  const coverage = review?.coverage || {};
  const activeAlerts = alertCount(metrics);
  const hasUnsavedChanges = Boolean(review?.config) && (
    config.baseline_run_id !== review.config.baseline_run_id
    || Object.keys(DEFAULTS).some((key) => config[key] !== review.config[key])
  );
  const baselineId = review?.baseline_run_id || review?.config?.baseline_run_id;
  const baseline = runs.find((run) => run.run_id === baselineId);
  return (
    <div className="drift-review">
      {!review && !reviewError && <p className="drift-review__empty" role="status">Loading review…</p>}
      {reviewError && <p role="alert" className="drift-review__error">{reviewError}</p>}
      {runsError && <p role="alert" className="drift-review__error">{runsError}</p>}
      {review?.status === "reviewed" && (
        <>
          <div className={`drift-review__verdict ${activeAlerts ? "is-alert" : ""}`} role="status" aria-live="polite">
            <strong>{activeAlerts ? `${activeAlerts} limit${activeAlerts === 1 ? "" : "s"} crossed` : "No limits crossed"}</strong>
            <span>{coverage.observed_predictions} observed forecasts · {coverage.matched} exact matches</span>
          </div>
          <p className="drift-review__baseline">Compared with <strong>{baseline?.name || baselineId}</strong></p>
          {hasUnsavedChanges && <p className="drift-review__pending" role="status">Unsaved changes. Readings still use the saved baseline and limits.</p>}
          {GROUPS.map(([title, keys]) => (
            <section className="drift-review__group" key={title} aria-label={title}>
              <h3>{title}</h3>
              <dl>{keys.filter((key) => metrics[key]).map((key) => <MetricRow key={key} name={key} metric={metrics[key]} />)}</dl>
            </section>
          ))}
          <p className="drift-review__coverage">{coverage.baseline_predictions} baseline forecasts · {coverage.orders_retained} recent orders · {coverage.fills_retained} recent fills</p>
        </>
      )}
      {review?.status === "awaiting_telemetry" && <p className="drift-review__empty" role="status">Waiting for observed strategy telemetry.</p>}
      {review?.status === "unconfigured" && <p className="drift-review__empty" role="status">Select a research run and save limits to start the review.</p>}
      {saveMessage && <p className="drift-review__saved" role="status">{saveMessage}</p>}
      <details className="drift-review__settings" key={review?.status === "unconfigured" ? "new" : "saved"} open={review?.status === "unconfigured" || undefined}>
        <summary>{review?.status === "unconfigured" ? "Configure review" : "Baseline and review limits"}</summary>
        <form onSubmit={save} className="drift-review__form">
          <label>Research baseline
            <select value={config.baseline_run_id} required onChange={(event) => { setConfig({ ...config, baseline_run_id: event.target.value }); setSaveMessage(""); }}>
              <option value="">Select a saved run</option>
              {runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.name || run.run_id} · {run.kind}</option>)}
            </select>
          </label>
          {runsLoading && <p className="drift-review__empty" role="status">Loading saved runs…</p>}
          {!runsLoading && !runs.length && !runsError && <p className="drift-review__empty">No saved {assetClass} research runs are available. Complete a backtest first.</p>}
          <div className="drift-review__limits">
            {LIMITS.map(([key, label, unit]) => (
              <label key={key}>{label} <span>({unit})</span>
                <input type="number" min={key === "min_matched" || key === "max_data_age_minutes" ? 1 : 0} max={key.endsWith("_pct") ? 100 : 10000} step={key === "min_matched" ? 1 : "any"} value={config[key]} onChange={(event) => { setConfig({ ...config, [key]: Number(event.target.value) }); setSaveMessage(""); }} />
              </label>
            ))}
          </div>
          <button type="submit" disabled={saving || !config.baseline_run_id || Boolean(review?.config) && !hasUnsavedChanges}>{saving ? "Saving…" : "Save review limits"}</button>
        </form>
      </details>
      {review?.status === "reviewed" && (
        <>
          <details className="drift-review__history">
            <summary>Alert history · {review.alerts?.length || 0}</summary>
            {review.alerts?.length ? (
              <ol>{[...review.alerts].reverse().map((alert, index) => (
                <li key={`${alert.ts}-${alert.metric}-${index}`}><time dateTime={alert.ts}>{new Date(alert.ts).toLocaleString()}</time> · {alert.baseline_run_id || "prior baseline"} · {LABELS[alert.metric] || alert.metric} {alert.state} {alert.value == null ? "" : `at ${alert.value}`}</li>
              ))}</ol>
            ) : <p className="drift-review__empty">No threshold crossings recorded.</p>}
          </details>
          <details className="drift-review__notes"><summary>Coverage and measurement notes</summary>{review.notes?.map((note) => <p key={note}>{note}</p>)}</details>
        </>
      )}
    </div>
  );
}
