import { useId } from "react";

import { formatNum, formatPct, formatUsd } from "../../lib/format.js";
import "./metrics-panel.css";

const BASE_ROWS = [
  { key: "objective_score", label: "Optimization score", fmt: (v) => formatNum(v, 3) },
  { key: "net_profit_usd", label: "Net Profit", fmt: formatUsd },
  { key: "sharpe_ratio", label: "Sharpe", fmt: (v) => formatNum(v, 2) },
  { key: "sortino_ratio", label: "Sortino", fmt: (v) => (typeof v === "number" ? formatNum(v, 2) : v) },
  { key: "max_drawdown_pct", label: "Max Drawdown", fmt: (v) => formatPct(v, 2) },
  { key: "win_rate_pct", label: "Win Rate", fmt: (v) => formatPct(v, 1) },
  { key: "profit_factor", label: "Profit Factor", fmt: (v) => (typeof v === "number" ? formatNum(v, 2) : v) },
  { key: "turnover_rate", label: "Turnover", fmt: (v) => `${formatNum(v, 2)}x` },
  { key: "total_trades", label: "Trades", fmt: (v) => v },
  // Only present for kind === "optimize" runs (see deriveChannels.js's
  // optunaTrialsCount) -- renders "—" for backtest runs via the null check
  // MetricsGrid already applies to every row.
  { key: "optuna_trials", label: "Optuna Trials", fmt: (v) => v },
];

// oos_r2/dir_acc/ic are PER-TICKER (see lib/deriveChannels.js's
// mlSummaryMetrics), unlike every other row in this panel, which is
// portfolio-wide -- the ticker is folded into each label so that scope
// difference stays visible even without a separate section/divider.
function mlRows(ticker) {
  return [
    { key: "oos_r2", label: `OOS R² (${ticker})`, fmt: (v) => formatNum(v, 3) },
    { key: "directional_accuracy", label: `Dir Acc (${ticker})`, fmt: (v) => formatPct(v * 100, 1) },
    { key: "information_coefficient", label: `IC (${ticker})`, fmt: (v) => formatNum(v, 3) },
  ];
}

const ZERO_POINT_ROWS = new Set([
  "net_profit_usd",
  "sharpe_ratio",
  "sortino_ratio",
  "objective_score",
  "oos_r2",
  "information_coefficient",
]);
// Directional accuracy has a 0.5 (coin-flip) zero-point, not 0.
const HALF_POINT_ROWS = new Set(["directional_accuracy"]);

function signClass(key, value) {
  if (typeof value !== "number") return "";
  if (ZERO_POINT_ROWS.has(key)) {
    return value > 0 ? "is-positive" : value < 0 ? "is-negative" : "";
  }
  if (key === "profit_factor") {
    return value > 1 ? "is-positive" : value < 1 ? "is-negative" : "";
  }
  if (HALF_POINT_ROWS.has(key)) {
    return value > 0.5 ? "is-positive" : value < 0.5 ? "is-negative" : "";
  }
  return "";
}

function MetricsGrid({ rows, values, primaryMetric }) {
  return (
    <dl className="metrics-panel__grid">
      {rows.map((row) => {
        const value = values[row.key];
        const cls = signClass(row.key, value);
        return (
          <div className={`metrics-panel__cell ${row.key === primaryMetric ? "is-primary" : ""}`} key={row.key}>
            <dt className="label">{row.label}{row.key === primaryMetric ? " · ratio − activity penalty" : ""}</dt>
            <dd className={`num metrics-panel__value ${cls}`}>
              {value == null ? "—" : row.fmt(value)}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export default function MetricsPanel({
  metrics,
  title = "Metrics",
  mlMetrics = null,
  mlTicker = null,
  trialsCount = null,
  objectiveMetric = null,
}) {
  const headingId = useId();
  if (!metrics) return null;
  const metric = objectiveMetric || metrics.scoring_metric || "sortino";
  const primaryKey = "objective_score";
  const ratioKey = metric === "sharpe" ? "sharpe_ratio" : "sortino_ratio";
  const reordered = [
    BASE_ROWS.find((row) => row.key === primaryKey),
    BASE_ROWS.find((row) => row.key === ratioKey),
    ...BASE_ROWS.filter((row) => row.key !== primaryKey && row.key !== ratioKey),
  ];
  const rows = mlMetrics ? [...reordered, ...mlRows(mlTicker)] : reordered;
  const fallbackRatio = metrics[ratioKey];
  const fallbackPenalty = metric === "sharpe" ? 0.001 : 0.002;
  const objectiveScore = metrics.objective_score ?? (
    typeof fallbackRatio === "number"
      ? fallbackRatio - fallbackPenalty * Number(metrics.total_trades || 0)
      : null
  );
  const values = {
    ...metrics,
    objective_score: objectiveScore,
    optuna_trials: trialsCount,
    ...(mlMetrics || {}),
  };
  return (
    <section className="metrics-panel" aria-labelledby={headingId}>
      <h2 id={headingId} className="label metrics-panel__heading">
        {title} · {metric.toUpperCase()} scoring profile
      </h2>
      <MetricsGrid rows={rows} values={values} primaryMetric={primaryKey} />
    </section>
  );
}
