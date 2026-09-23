import { useEffect, useMemo, useRef, useState } from "react";

import ActionPanel from "../bezel/ActionPanel.jsx";
import ActiveResearchRun from "./ActiveResearchRun.jsx";
import CampaignPanel from "./CampaignPanel.jsx";
import ChannelStrip from "../channel-strip/ChannelStrip.jsx";
import ModelDecisionTape from "../model-tape/ModelDecisionTape.jsx";
import RunList from "../run-list/RunList.jsx";
import { api } from "../../lib/api.js";
import { formatDate, formatNum, formatPct, formatUsd } from "../../lib/format.js";
import { DASHBOARD_THEMES } from "../../lib/theme.js";
import { isJobActive } from "../../lib/jobs.js";
import "./research-hub.css";

const DRAWDOWN_THRESHOLDS = [
  { value: -5, label: "DRAWDOWN WARN 5%", kind: "warn" },
  { value: -10, label: "KILL-SWITCH 10%", kind: "danger" },
];

const METRIC_ROWS = [
  ["total_return_pct", "Total return", "%"],
  ["cagr_pct", "CAGR", "%"],
  ["annual_volatility_pct", "Annual volatility", "%"],
  ["sharpe", "Sharpe ratio", "ratio"],
  ["sortino", "Sortino ratio", "ratio"],
  ["max_drawdown_pct", "Maximum drawdown", "%"],
  ["calmar", "Calmar ratio", "ratio"],
  ["ulcer_index_pct", "Ulcer index", "%"],
];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function toPoints(rows = []) {
  return rows.map((point) => ({ x: Date.parse(point.ts), y: point.value }));
}

function metricValue(value, type = "ratio") {
  if (value == null || Number.isNaN(value)) return "—";
  return type === "%" ? formatPct(value, 2) : formatNum(value, 2);
}

function signedClass(value, inverse = false) {
  if (value == null || value === 0) return "";
  const positive = inverse ? value < 0 : value > 0;
  return positive ? "is-positive" : "is-negative";
}

function compactPeriod(period) {
  const start = Date.parse(period?.start);
  const end = Date.parse(period?.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const startYear = new Date(start).getUTCFullYear();
  const endYear = new Date(end).getUTCFullYear();
  return startYear === endYear ? String(startYear) : `${startYear}–${endYear}`;
}

function compactBarInterval(minutes) {
  if (!Number.isFinite(Number(minutes))) return null;
  const value = Number(minutes);
  if (value === 1440) return "1 DAY BARS";
  if (value % 60 === 0) return `${value / 60}H BARS`;
  return `${value} MIN BARS`;
}

function Panel({ title, note, children, className = "" }) {
  return (
    <section className={`research-panel ${className}`}>
      <header className="research-panel__header">
        <h2>{title}</h2>
        {note && <p>{note}</p>}
      </header>
      <div className="research-panel__body">{children}</div>
    </section>
  );
}

function EvidenceHeader({ run, research, workflow, compareRun, profile }) {
  const metrics = run?.metrics || run?.oos_metrics || {};
  const isOptimize = workflow === "optimize";
  const excessReturn = research?.benchmark?.status === "ready"
    ? (research.strategy?.total_return_pct ?? 0) - (research.benchmark.metrics?.total_return_pct ?? 0)
    : null;
  const runTitle = run?.name?.trim() || run?.run_id || "No run selected";
  const runDetails = run
    ? [
        run.name ? run.run_id : null,
        (run.tickers || []).join(" · "),
        `${formatDate(research?.period?.start)}—${formatDate(research?.period?.end)}`,
      ].filter(Boolean).join(" · ")
    : "Run a study or select a saved result.";
  const badges = [
    isOptimize ? "OUTER HOLDOUT" : "HISTORICAL SIMULATION",
    profile?.short_label || run?.asset_class,
    compactPeriod(research?.period),
    compactBarInterval(run?.bar_interval_minutes),
    run?.market_session || run?.asset_profile?.market?.session,
    metrics.total_trades != null ? `${metrics.total_trades} TRADES` : null,
  ].filter(Boolean);
  return (
    <section className="research-verdict" aria-label="Selected run evidence summary">
      <div className="research-verdict__identity">
        <div className="research-verdict__badges">
          {badges.map((badge) => <span key={badge}>{String(badge).toUpperCase()}</span>)}
          {compareRun && <span className="is-comparison">VS {compareRun.name?.trim() || compareRun.run_id}</span>}
        </div>
        <h1 title={runTitle}>{runTitle}</h1>
        <p>{runDetails}</p>
      </div>
      <dl className="research-verdict__readings">
        <div>
          <dt>{run?.objective_metric?.toUpperCase() || "PRIMARY"}</dt>
          <dd className={signedClass(metrics.primary_score)}>{metricValue(metrics.primary_score)}</dd>
        </div>
        <div>
          <dt>NET RESULT</dt>
          <dd className={signedClass(metrics.net_profit_usd)}>{formatUsd(metrics.net_profit_usd)}</dd>
        </div>
        <div>
          <dt>VS S&amp;P 500</dt>
          <dd className={signedClass(excessReturn)}>{excessReturn == null ? "—" : formatPct(excessReturn, 2)}</dd>
        </div>
        <div>
          <dt>MAX DRAWDOWN</dt>
          <dd className={signedClass(metrics.max_drawdown_pct, true)}>{metricValue(metrics.max_drawdown_pct, "%")}</dd>
        </div>
      </dl>
    </section>
  );
}

function MetricLedger({ research }) {
  const strategy = research?.strategy || {};
  const benchmark = research?.benchmark?.metrics || {};
  return (
    <div className="metric-ledger">
      <div className="metric-ledger__head" aria-hidden="true">
        <span>Measure</span><span>Strategy</span><span>S&amp;P 500</span><span>Difference</span>
      </div>
      {METRIC_ROWS.map(([key, label, type]) => {
        const left = strategy[key];
        const right = benchmark[key];
        const delta = left != null && right != null ? left - right : null;
        const inverse = key.includes("drawdown") || key.includes("ulcer") || key.includes("volatility");
        return (
          <div className="metric-ledger__row" key={key}>
            <span>{label}</span>
            <strong className="num">{metricValue(left, type)}</strong>
            <span className="num">{metricValue(right, type)}</span>
            <span className={`num ${signedClass(delta, inverse)}`}>{delta == null ? "—" : `${delta > 0 ? "+" : ""}${metricValue(delta, type)}`}</span>
          </div>
        );
      })}
    </div>
  );
}

function RelativeEvidence({ research, run }) {
  const relative = research?.relative || {};
  const validation = research?.optimization?.validation || {};
  const evidence = [
    ["Alpha (annual)", metricValue(relative.alpha_annual_pct, "%"), relative.alpha_annual_pct > 0],
    ["Information ratio", metricValue(relative.information_ratio), relative.information_ratio > 0],
    ["Beta to S&P", metricValue(relative.beta), Math.abs(relative.beta ?? 1) < 0.5],
    ["PSR > S&P", metricValue(relative.probabilistic_sharpe_vs_benchmark_pct, "%"), relative.probabilistic_sharpe_vs_benchmark_pct >= 95],
    ["Stress costs", validation.final_test?.stressed_ratio == null ? "Not recorded" : metricValue(validation.final_test.stressed_ratio), validation.final_test?.stressed_ratio > 0],
    ["Sample size", `${research?.strategy?.observations ?? 0} daily returns`, (research?.strategy?.observations ?? 0) >= 252],
  ];
  return (
    <div className="evidence-ledger">
      {evidence.map(([label, value, passed]) => (
        <div key={label}>
          <span className={`evidence-ledger__mark ${passed ? "is-pass" : "is-review"}`} aria-hidden="true" />
          <span className="sr-only">{passed ? "Pass" : "Review"}: </span>
          <span>{label}</span>
          <strong className="num">{value}</strong>
        </div>
      ))}
      {run?.kind === "optimize" && (
        <p>Selection score and outer-holdout result remain separate. Promotion still requires the multi-seed robustness campaign and paper agreement.</p>
      )}
    </div>
  );
}

function MonthlyMatrix({ rows = [] }) {
  const years = [...new Set(rows.map((row) => row.year))].sort((a, b) => b - a);
  const byKey = new Map(rows.map((row) => [`${row.year}-${row.month}`, row.return_pct]));
  if (!years.length) return <div className="research-empty">Monthly return history is unavailable.</div>;
  return (
    <div className="monthly-matrix" role="table" aria-label="Monthly strategy returns">
      <div className="monthly-matrix__row is-head" role="row">
        <span role="columnheader">Year</span>
        {MONTHS.map((month) => <span role="columnheader" key={month}>{month}</span>)}
      </div>
      {years.map((year) => (
        <div className="monthly-matrix__row" role="row" key={year}>
          <strong role="rowheader">{year}</strong>
          {MONTHS.map((month, index) => {
            const value = byKey.get(`${year}-${index + 1}`);
            const intensity = value == null ? 0 : Math.min(1, Math.abs(value) / 8);
            return (
              <span
                role="cell"
                key={month}
                className={value == null ? "is-empty" : value >= 0 ? "is-gain" : "is-loss"}
                style={{ "--return-strength": `${12 + intensity * 52}%` }}
                title={value == null ? `${month} ${year}: unavailable` : `${month} ${year}: ${formatPct(value, 2)}`}
              >
                {value == null ? "·" : `${value > 0 ? "+" : ""}${value.toFixed(1)}`}
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function TradeSummary({ research, run }) {
  const trade = research?.trades || {};
  const metrics = run?.metrics || run?.oos_metrics || {};
  const rows = [
    ["Closed trades", trade.count || metrics.closed_positions],
    ["Win rate", metricValue(metrics.win_rate_pct, "%")],
    ["Expectancy", formatUsd(trade.expectancy_usd)],
    ["Average win", formatUsd(trade.average_win_usd)],
    ["Average loss", formatUsd(trade.average_loss_usd)],
    ["Payoff ratio", metricValue(trade.payoff_ratio)],
    ["Profit factor", typeof metrics.profit_factor === "number" ? metricValue(metrics.profit_factor) : metrics.profit_factor || "—"],
    ["Worst loss", formatUsd(trade.largest_loss_usd)],
    ["Loss streak", trade.max_loss_streak ?? "—"],
    ["Turnover", metrics.turnover_rate == null ? "—" : `${metricValue(metrics.turnover_rate)}x`],
  ];
  return (
    <dl className="trade-summary">
      {rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="num">{value}</dd></div>)}
    </dl>
  );
}

function TrialHistory({ optimization }) {
  const history = optimization?.history || [];
  const complete = history.filter((trial) => trial.value != null).map((trial) => ({ x: trial.number, y: trial.value }));
  const incumbent = history.filter((trial) => trial.incumbent != null).map((trial) => ({ x: trial.number, y: trial.incumbent }));
  return (
    <ChannelStrip
      label="OBJECTIVE / TRIAL"
      color="var(--color-text-primary)"
      series={complete}
      overlaySeries={incumbent}
      overlayColor="var(--color-text-dim)"
      overlayLabel="incumbent"
      currentValueLabel={complete.length ? formatNum(complete.at(-1).y, 3) : "—"}
      emptyMessage="No completed Optuna trials recorded"
      tickFormat={(value) => formatNum(value, 2)}
      height={250}
    />
  );
}

function ParameterImportance({ optimization }) {
  const rows = optimization?.parameter_importance || [];
  if (!rows.length) return <div className="research-empty">At least three completed trials are required for sensitivity estimates.</div>;
  return (
    <div className="parameter-importance">
      {rows.slice(0, 12).map((row) => (
        <div key={row.parameter}>
          <span>{row.parameter}</span>
          <span className="parameter-importance__track"><i style={{ width: `${Math.max(2, row.importance * 100)}%` }} /></span>
          <strong className="num">{formatNum(row.importance, 3)}</strong>
        </div>
      ))}
      <p>Absolute Spearman association with trial objective. Use it to locate sensitivity, not as causal feature importance.</p>
    </div>
  );
}

function FoldMatrix({ optimization }) {
  const folds = optimization?.folds || [];
  if (!folds.length) return <div className="research-empty">Fold-level outcomes were not recorded for this run.</div>;
  return (
    <div className="research-table-wrap">
      <table className="research-table">
        <thead><tr><th>Fold</th><th>Period</th><th>Normal</th><th>2× costs</th><th>Degradation</th><th>Turnover</th><th>Trades</th></tr></thead>
        <tbody>
          {folds.map((fold, index) => {
            const normal = fold.normal_ratio ?? fold.normal;
            const stress = fold.stressed_ratio ?? fold.stressed;
            return (
              <tr key={fold.fold ?? index}>
                <th>{fold.fold ?? index + 1}</th>
                <td>{formatDate(fold.validation_start)}—{formatDate(fold.validation_end)}</td>
                <td className={`num ${signedClass(normal)}`}>{metricValue(normal)}</td>
                <td className={`num ${signedClass(stress)}`}>{metricValue(stress)}</td>
                <td className="num">{normal != null && stress != null ? metricValue(normal - stress) : "—"}</td>
                <td className="num">{metricValue(fold.turnover)}</td>
                <td className="num">{fold.trades ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function BestParameters({ optimization }) {
  const params = Object.entries(optimization?.best_params || {});
  return (
    <div className="best-parameters">
      {params.map(([key, value]) => (
        <div key={key}><span>{key}</span><strong className="num">{typeof value === "number" ? formatNum(value, Number.isInteger(value) ? 0 : 5) : String(value)}</strong></div>
      ))}
    </div>
  );
}

export default function ResearchHub({
  workflow,
  profile,
  assetProfiles,
  assetClass,
  onAssetClassChange,
  runs,
  allRuns,
  activeRunId,
  compareRunId,
  activeRun,
  compareRun,
  onSelectRun,
  onCompareRun,
  onDeleteRun,
  runLoadStatus,
  jobs,
  onJobStarted,
  onJobStopped,
  brokerStatus,
  toolbarNavigation,
  toolbarLead,
  toolbarStatus,
  theme,
  onThemeChange,
  ticker,
  onTickerChange,
}) {
  const [section, setSection] = useState("overview");
  const [controlsOpen, setControlsOpen] = useState(false);
  const [activeJobViewId, setActiveJobViewId] = useState(null);
  const [research, setResearch] = useState(null);
  const [compareResearch, setCompareResearch] = useState(null);
  const [researchStatus, setResearchStatus] = useState("idle");
  const [researchError, setResearchError] = useState(null);
  const controlsRef = useRef(null);
  const activeDrawer = controlsOpen ? "controls" : null;

  useEffect(() => {
    setSection("overview");
    setActiveJobViewId(null);
  }, [workflow, assetClass]);

  useEffect(() => {
    if (!activeDrawer) return undefined;
    const drawer = controlsRef.current;
    const previousFocus = document.activeElement;
    const selector = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
    const focusable = () => [...(drawer?.querySelectorAll(selector) || [])];
    focusable()[0]?.focus();

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        setControlsOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus?.();
    };
  }, [activeDrawer]);

  useEffect(() => {
    if (!activeRunId) {
      setResearch(null);
      setResearchStatus("idle");
      return;
    }
    let disposed = false;
    setResearchStatus("loading");
    setResearchError(null);
    api.getRunResearch(activeRunId)
      .then((result) => {
        if (!disposed) {
          setResearch(result);
          setResearchStatus("ready");
        }
      })
      .catch((error) => {
        if (!disposed) {
          setResearch(null);
          setResearchStatus("error");
          setResearchError(error.message);
        }
      });
    return () => { disposed = true; };
  }, [activeRunId]);

  useEffect(() => {
    if (!compareRunId) {
      setCompareResearch(null);
      return;
    }
    let disposed = false;
    api.getRunResearch(compareRunId)
      .then((result) => { if (!disposed) setCompareResearch(result); })
      .catch(() => { if (!disposed) setCompareResearch(null); });
    return () => { disposed = true; };
  }, [compareRunId]);

  const sections = useMemo(() => [
    ["overview", "Evidence"],
    ["performance", "Performance"],
    ["risk", "Risk & trades"],
    ["model", "Model"],
    ...(workflow === "optimize" ? [["optimization", "Optimization"]] : []),
  ], [workflow]);
  const activeResearchJobs = useMemo(
    () => jobs.filter((job) => (
      !job.parent_job_id
      && (job.kind === workflow || job.kind === "data_repair")
      && isJobActive(job)
      && (job.config?.asset_class || "crypto") === assetClass
    )),
    [assetClass, jobs, workflow],
  );
  const activeJobView = jobs.find((job) => job.id === activeJobViewId) || null;

  useEffect(() => {
    if (
      activeJobView?.status === "completed"
      && activeJobView.run_id
      && runs.some((run) => run.run_id === activeJobView.run_id)
    ) {
      onSelectRun(activeJobView.run_id);
      setActiveJobViewId(null);
    }
  }, [activeJobView, onSelectRun, runs]);
  const strategyIndex = toPoints(research?.series?.strategy_index);
  const benchmarkIndex = toPoints(research?.series?.benchmark_index);
  const compareIndex = toPoints(compareResearch?.series?.strategy_index);
  const strategyDrawdown = toPoints(research?.series?.strategy_drawdown);
  const benchmarkDrawdown = toPoints(research?.series?.benchmark_drawdown);
  const rollingSharpe = toPoints(research?.series?.rolling_sharpe_63d);
  const tickers = activeRun?.tickers || [];
  const modelChart = ticker ? activeRun?.model_chart?.[ticker] || [] : [];
  const actionPanel = (
    <ActionPanel
      onJobStarted={(job) => {
        onJobStarted(job);
        setActiveJobViewId(job.id);
        setControlsOpen(false);
      }}
      onJobStopped={onJobStopped}
      workflow={workflow}
      assetClass={assetClass}
      assetProfiles={assetProfiles}
      onAssetClassChange={onAssetClassChange}
      runs={allRuns}
      jobs={jobs}
      brokerStatus={brokerStatus}
    />
  );

  return (
    <section className="research-hub">
      <header className="research-hub__toolbar" inert={activeDrawer ? "" : undefined}>
        <div className="research-hub__nav">{toolbarNavigation}</div>
        <div className="research-hub__title">
          <strong>{workflow === "optimize" ? "Optimization research" : "Backtest research"}</strong>
          <span>{profile?.short_label} · evidence workbench</span>
        </div>
        <div className="research-hub__profile">{toolbarLead}</div>
        <div className="research-hub__theme" role="group" aria-label="Dashboard theme">
          {DASHBOARD_THEMES.map((option) => (
            <button key={option.id} type="button" aria-label={`${option.label} theme`} aria-pressed={theme === option.id} className={`is-${option.id}`} onClick={() => onThemeChange(option.id)} />
          ))}
        </div>
        <div className="research-hub__status">{toolbarStatus}</div>
      </header>

      <aside className="research-hub__library" aria-label="Research run library" inert={activeDrawer ? "" : undefined}>
        <div className="research-hub__launch-slot">
          <button type="button" className="research-hub__new-run" onClick={() => setControlsOpen(true)}>
            <span className="research-hub__new-run-mark" aria-hidden="true">+</span>
            <span>
              <strong>New {workflow === "optimize" ? "sweep" : "backtest"}</strong>
              <small>Configure data, model, and risk</small>
            </span>
          </button>
        </div>
        <RunList
          runs={runs}
          activeJobs={activeResearchJobs}
          activeRunId={activeRunId}
          compareRunId={compareRunId}
          selectedJobId={activeJobViewId}
          onSelect={(runId) => {
            setActiveJobViewId(null);
            if (runId === compareRunId) onCompareRun(null);
            onSelectRun(runId);
          }}
          onSelectJob={setActiveJobViewId}
          onCompare={onCompareRun}
          onDelete={onDeleteRun}
          loadStatus={runLoadStatus}
        />
      </aside>

      <div className="research-hub__main" inert={activeDrawer ? "" : undefined}>
        {activeJobView && <ActiveResearchRun job={activeJobView} onJobUpdated={onJobStopped} />}
        {!activeJobView && <EvidenceHeader run={activeRun} research={research} workflow={workflow} compareRun={compareRun} profile={profile} />}
        {!activeJobView && <nav className="research-hub__sections" aria-label="Research report sections">
          {sections.map(([key, label]) => (
            <button key={key} type="button" aria-current={section === key ? "page" : undefined} onClick={() => setSection(key)}>{label}</button>
          ))}
        </nav>}

        {!activeJobView && researchStatus === "loading" && <div className="research-hub__loading">Building benchmark-relative analysis…</div>}
        {!activeJobView && researchStatus === "error" && <div className="research-hub__error" role="alert">Research analysis unavailable: {researchError}</div>}
        {!activeJobView && !activeRun && <div className="research-hub__blank"><strong>No saved {workflow} selected.</strong><button type="button" onClick={() => setControlsOpen(true)}>Configure the first run</button></div>}

        {!activeJobView && activeRun && section === "overview" && (
          <div className="research-hub__content-grid">
            <Panel title="Benchmark ledger" note="Same observed period · both series rebased to 100" className="is-wide">
              <ChannelStrip label="GROWTH OF 100" color="var(--color-trace-amber)" series={strategyIndex} ghostSeries={compareIndex.length ? compareIndex : null} ghostLabel={compareRun?.name || compareRun?.run_id} overlaySeries={benchmarkIndex} overlayColor="var(--color-text-primary)" overlayLabel="S&P 500 price index" currentValueLabel={strategyIndex.length ? formatNum(strategyIndex.at(-1).y, 1) : "—"} emptyMessage="Equity curve unavailable" height={300} tickFormat={(value) => formatNum(value, 0)} />
              {research?.benchmark?.status !== "ready" && <p className="research-panel__notice">{research?.benchmark?.message || "Benchmark is unavailable."}</p>}
            </Panel>
            <Panel title="Performance ledger" note="Strategy against an independent market comparator"><MetricLedger research={research} /></Panel>
            <Panel title="Dataset & universe" note="Retained input version and declared historical coverage" className="is-wide"><DatasetProvenance evidence={activeRun.dataset_provenance} /></Panel>
            <Panel title="Evidence checks" note="Research questions, not promotion guarantees"><RelativeEvidence research={research} run={activeRun} /></Panel>
          </div>
        )}

        {!activeJobView && activeRun && section === "performance" && (
          <div className="research-hub__content-grid">
            <Panel title="Cumulative performance" note="Strategy, selected comparison run, and S&P 500" className="is-wide">
              <ChannelStrip label="INDEX" color="var(--color-trace-amber)" series={strategyIndex} ghostSeries={compareIndex.length ? compareIndex : null} ghostLabel={compareRun?.name || compareRun?.run_id} overlaySeries={benchmarkIndex} overlayColor="var(--color-text-primary)" overlayLabel="S&P 500" currentValueLabel={strategyIndex.length ? formatNum(strategyIndex.at(-1).y, 1) : "—"} height={330} tickFormat={(value) => formatNum(value, 0)} />
            </Panel>
            <Panel title="Rolling Sharpe" note={research?.sharpe_basis || "63-day trailing estimate; instability should remain visible"}>
              <ChannelStrip label="63D SHARPE" color="var(--color-trace-amber)" series={rollingSharpe} currentValueLabel={rollingSharpe.length ? formatNum(rollingSharpe.at(-1).y, 2) : "—"} height={240} emptyMessage="More daily observations are required" tickFormat={(value) => formatNum(value, 1)} />
            </Panel>
            <Panel title="Monthly return map" note="Calendar pattern and concentration of returns" className="is-wide"><MonthlyMatrix rows={research?.monthly_returns} /></Panel>
            {activeRun.asset_class === "equity" && <Panel title="Execution & cost evidence" note="Locked assumptions and realized simulation accounting" className="is-wide"><SimulationEvidence run={activeRun} /></Panel>}
          </div>
        )}

        {!activeJobView && activeRun && section === "risk" && (
          <div className="research-hub__content-grid">
            <Panel title="Underwater curve" note="Depth and duration of every drawdown" className="is-wide">
              <ChannelStrip label="DRAWDOWN" color="var(--color-trace-amber)" series={strategyDrawdown} overlaySeries={benchmarkDrawdown} overlayColor="var(--color-text-primary)" overlayLabel="S&P 500" thresholds={DRAWDOWN_THRESHOLDS} currentValueLabel={strategyDrawdown.length ? formatPct(strategyDrawdown.at(-1).y, 2) : "—"} height={310} tickFormat={(value) => formatPct(value, 1)} />
            </Panel>
            <Panel title="Trade diagnostics" note="Closed-position outcome quality"><TradeSummary research={research} run={activeRun} /></Panel>
            <Panel title="Tail and path risk" note="Daily return distribution">
              <dl className="trade-summary">
                <div><dt>Worst day</dt><dd className="num">{metricValue(research?.strategy?.worst_day_pct, "%")}</dd></div>
                <div><dt>Best day</dt><dd className="num">{metricValue(research?.strategy?.best_day_pct, "%")}</dd></div>
                <div><dt>Skew</dt><dd className="num">{metricValue(research?.strategy?.skew)}</dd></div>
                <div><dt>Excess kurtosis</dt><dd className="num">{metricValue(research?.strategy?.excess_kurtosis)}</dd></div>
                <div><dt>Ulcer index</dt><dd className="num">{metricValue(research?.strategy?.ulcer_index_pct, "%")}</dd></div>
                <div><dt>PSR &gt; 0</dt><dd className="num">{metricValue(research?.relative?.probabilistic_sharpe_vs_zero_pct, "%")}</dd></div>
              </dl>
            </Panel>
          </div>
        )}

        {!activeJobView && activeRun && section === "model" && (
          <div className="research-hub__content-grid">
            <Panel title="Walk-forward model tape" note="Price, forecast, regime, and decision context" className="is-wide research-panel--model">
              {tickers.length > 1 && <label className="research-hub__ticker">Model symbol<select value={ticker || ""} onChange={(event) => onTickerChange(event.target.value)}>{tickers.map((symbol) => <option key={symbol}>{symbol}</option>)}</select></label>}
              <ModelDecisionTape points={modelChart} ticker={ticker || "—"} model={{ ...activeRun?.model_chart_meta, entry_threshold: modelChart.at(-1)?.entry_threshold, protective_orders_submitted: false }} assetClass={assetClass} />
            </Panel>
            <Panel title="Prediction quality" note="Per-symbol walk-forward statistics">
              <div className="research-table-wrap"><table className="research-table"><thead><tr><th>Symbol</th><th>OOS R²</th><th>Direction</th><th>IC</th><th>Samples</th></tr></thead><tbody>{tickers.map((symbol) => { const item = activeRun?.ml_performance?.[symbol] || {}; return <tr key={symbol}><th>{symbol}</th><td className={`num ${signedClass(item.oos_r2)}`}>{metricValue(item.oos_r2)}</td><td className="num">{item.directional_accuracy == null ? "—" : formatPct(item.directional_accuracy * 100, 1)}</td><td className={`num ${signedClass(item.information_coefficient)}`}>{metricValue(item.information_coefficient)}</td><td className="num">{item.oos_samples ?? "—"}</td></tr>; })}</tbody></table></div>
            </Panel>
          </div>
        )}

        {!activeJobView && workflow === "optimize" && section === "optimization" && (
          <div className="research-hub__content-grid">
            <Panel title="Validation campaign" note="Seed consensus → robustness → one-shot outer holdout" className="is-wide"><CampaignPanel key={assetClass} assetClass={assetClass} profile={profile} jobs={jobs} onJobStarted={onJobStarted} /></Panel>
            {activeRun && <Panel title="Search history" note="Every trial and the running incumbent" className="is-wide"><TrialHistory optimization={research?.optimization} /></Panel>}
            {activeRun && <Panel title="Parameter sensitivity" note="Search-space association"><ParameterImportance optimization={research?.optimization} /></Panel>}
            {activeRun && <Panel title="Selected parameters" note={`Trial ${research?.optimization?.best_trial_number ?? "—"}`}><BestParameters optimization={research?.optimization} /></Panel>}
            {activeRun && <Panel title="Walk-forward and cost stress" note="Development folds only; outer holdout is kept separate" className="is-wide"><FoldMatrix optimization={research?.optimization} /></Panel>}
          </div>
        )}

      </div>

      <div className={`research-controls ${controlsOpen ? "is-open" : ""}`} aria-hidden={!controlsOpen}>
        <button type="button" className="research-controls__backdrop" aria-label="Close run configuration" onClick={() => setControlsOpen(false)} />
        <aside
          ref={controlsRef}
          className="research-controls__drawer"
          role="dialog"
          aria-modal="true"
          aria-labelledby="research-controls-title"
          inert={controlsOpen ? undefined : ""}
        >
          <header><div><span>{workflow === "optimize" ? "OPTIMIZATION PROTOCOL" : "BACKTEST PROTOCOL"}</span><h2 id="research-controls-title">Configure and launch</h2></div><button type="button" aria-label="Close run configuration" onClick={() => setControlsOpen(false)}><span className="research-controls__close-icon" aria-hidden="true"><i /><i /></span></button></header>
          {actionPanel}
        </aside>
      </div>

    </section>
  );
}
import SimulationEvidence from "./SimulationEvidence.jsx";

import DatasetProvenance from "./DatasetProvenance.jsx";
