import { useEffect, useMemo, useState } from "react";

import "./App.css";
import ActionPanel from "./components/bezel/ActionPanel.jsx";
import InstrumentBezel from "./components/bezel/InstrumentBezel.jsx";
import ChannelStrip from "./components/channel-strip/ChannelStrip.jsx";
import JobConsole from "./components/job-console/JobConsole.jsx";
import LivePanel from "./components/live-panel/LivePanel.jsx";
import MetricsPanel from "./components/metrics/MetricsPanel.jsx";
import RunList from "./components/run-list/RunList.jsx";
import { useInterval } from "./hooks/useInterval.js";
import { api } from "./lib/api.js";
import {
  actualPricePoints,
  drawdownPoints,
  equityPoints,
  predictedPricePoints,
  regimePoints,
  runMetrics,
  runTickers,
  mlSummaryMetrics,
  optunaTrialsCount,
} from "./lib/deriveChannels.js";
import { formatUsd } from "./lib/format.js";

const DRAWDOWN_THRESHOLDS = [
  { value: -5, label: "DRAWDOWN WARN 5%", kind: "warn" },
  { value: -10, label: "KILL-SWITCH 10%", kind: "danger" },
];
const formatUsdTick = (value) => formatUsd(value);
const formatPctTick = (value) => `${value.toFixed(1)}%`;
const formatScoreTick = (value) => value.toFixed(2);

function sameCollection(current, next, keys) {
  return (
    current.length === next.length &&
    current.every((item, index) => keys.every((key) => item[key] === next[index]?.[key]))
  );
}

export default function App() {
  const [runs, setRuns] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [dataStatus, setDataStatus] = useState({ runs: "loading", jobs: "loading" });
  const [dataErrors, setDataErrors] = useState({ runs: null, jobs: null, detail: null });
  const [brokerStatus, setBrokerStatus] = useState({ status: "loading" });
  const [activeRunId, setActiveRunId] = useState(null);
  const [compareRunId, setCompareRunId] = useState(null);
  const [activeRun, setActiveRun] = useState(null);
  const [compareRun, setCompareRun] = useState(null);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [ticker, setTicker] = useState(null);
  const [workflowTab, setWorkflowTab] = useState("backtest");

  const workflowCopy = {
    backtest: {
      title: "Run control & signal review",
      description: "Select a recorded run to inspect its risk rails, model performance, and regime traces.",
    },
    optimize: {
      title: "Optimize the strategy",
      description: "Search the strategy parameters, then compare in-sample and out-of-sample performance.",
    },
    paper: {
      title: "Paper Trading",
      description: "Connect to paper TWS or Gateway and monitor the same strategy before risking capital.",
    },
    live: {
      title: "Live Trading",
      description: "Review the live connection and risk rails before deploying real capital.",
    },
  }[workflowTab];

  async function refreshRuns() {
    try {
      const list = await api.listRuns();
      setRuns((current) =>
        sameCollection(current, list, ["run_id", "finished_at"]) ? current : list,
      );
      setActiveRunId((current) => current ?? list[0]?.run_id ?? null);
      setDataStatus((current) => ({ ...current, runs: "ready" }));
      setDataErrors((current) => ({ ...current, runs: null }));
    } catch (error) {
      setDataStatus((current) => ({ ...current, runs: "error" }));
      setDataErrors((current) => ({
        ...current,
        runs: `Run data unavailable: ${error.message}. Existing results may be stale.`,
      }));
    }
  }

  async function refreshJobs() {
    try {
      const list = await api.listJobs();
      setJobs((current) =>
        sameCollection(current, list, ["id", "status", "finished_at"]) ? current : list,
      );
      setDataStatus((current) => ({ ...current, jobs: "ready" }));
      setDataErrors((current) => ({ ...current, jobs: null }));
    } catch (error) {
      setDataStatus((current) => ({ ...current, jobs: "error" }));
      setDataErrors((current) => ({
        ...current,
        jobs: `Job data unavailable: ${error.message}. Job status is unknown.`,
      }));
    }
  }

  async function refreshBrokerStatus() {
    try {
      setBrokerStatus(await api.getBrokerStatus());
    } catch (error) {
      setBrokerStatus({ status: "unknown", message: `Broker status unavailable: ${error.message}` });
    }
  }

  async function handleDeleteRun(runId) {
    try {
      await api.deleteRun(runId);
    } catch (err) {
      window.alert(`Failed to delete run: ${err.message}`);
      return;
    }
    setRuns((prev) => prev.filter((r) => r.run_id !== runId));
    setActiveRunId((current) => (current === runId ? null : current));
    setCompareRunId((current) => (current === runId ? null : current));
  }

  useEffect(() => {
    refreshRuns();
    refreshJobs();
    refreshBrokerStatus();
  }, []);

  useInterval(refreshJobs, 3000);
  useInterval(refreshRuns, 8000);
  useInterval(refreshBrokerStatus, 3000);

  useEffect(() => {
    if (!activeRunId) {
      setActiveRun(null);
      return;
    }
    api
      .getRun(activeRunId)
      .then((run) => {
        setActiveRun(run);
        setDataErrors((current) => ({ ...current, detail: null }));
      })
      .catch((error) => {
        setActiveRun(null);
        setDataErrors((current) => ({
          ...current,
          detail: `Could not load run ${activeRunId}: ${error.message}`,
        }));
      });
  }, [activeRunId]);

  useEffect(() => {
    if (!compareRunId) {
      setCompareRun(null);
      return;
    }
    api
      .getRun(compareRunId)
      .then((run) => {
        setCompareRun(run);
        setDataErrors((current) => ({ ...current, detail: null }));
      })
      .catch((error) => {
        setCompareRun(null);
        setDataErrors((current) => ({
          ...current,
          detail: `Could not load comparison run ${compareRunId}: ${error.message}`,
        }));
      });
  }, [compareRunId]);

  const tickers = useMemo(() => runTickers(activeRun), [activeRun]);

  useEffect(() => {
    if (tickers.length && !tickers.includes(ticker)) {
      setTicker(tickers[0]);
    }
  }, [tickers, ticker]);

  function handleJobStarted(job) {
    setJobs((prev) => [job, ...prev]);
    setSelectedJobId(job.id);
    if (job.run_id) {
      // Backtest/optimize jobs write a run artifact once they finish; keep
      // polling the run list so the new run appears without a manual refresh.
      const poll = setInterval(async () => {
        const detail = await api.getJob(job.id).catch(() => null);
        if (detail && detail.status !== "running") {
          clearInterval(poll);
          if (detail.status === "completed") {
            refreshRuns();
            setActiveRunId(job.run_id);
          }
        }
      }, 2000);
    }
  }

  function handleJobStopped(job) {
    if (!job?.id) return;
    setJobs((current) => current.map((item) => (item.id === job.id ? job : item)));
  }

  const isResearchTab = workflowTab === "backtest" || workflowTab === "optimize";
  const runningJobCount = jobs.filter((j) => j.status === "running").length;
  const connectionStatus =
    dataStatus.runs === "error" || dataStatus.jobs === "error"
      ? "unknown"
      : dataStatus.runs === "loading" || dataStatus.jobs === "loading"
        ? "loading"
        : "ready";
  const dataError = Object.values(dataErrors).filter(Boolean).join(" ");
  const pageDataError = isResearchTab ? dataError : null;

  const equitySeries = useMemo(() => equityPoints(activeRun), [activeRun]);
  const compareEquitySeries = useMemo(() => equityPoints(compareRun), [compareRun]);
  const drawdownSeries = useMemo(() => drawdownPoints(activeRun), [activeRun]);
  const compareDrawdownSeries = useMemo(() => drawdownPoints(compareRun), [compareRun]);
  const actualSeries = useMemo(
    () => (ticker ? actualPricePoints(activeRun, ticker) : []),
    [activeRun, ticker],
  );
  const predictedSeries = useMemo(
    () => (ticker ? predictedPricePoints(activeRun, ticker) : []),
    [activeRun, ticker],
  );
  const regimeSeries = useMemo(
    () => (ticker ? regimePoints(activeRun, ticker) : []),
    [activeRun, ticker],
  );

  return (
    <div className={`app ${workflowTab === "live" ? "is-live" : ""}`}>
      <a className="skip-link" href="#dashboard-main">
        Skip to dashboard
      </a>
      <InstrumentBezel
        runningJobCount={runningJobCount}
        brokerStatus={brokerStatus}
      />

      <main
        id="dashboard-main"
        className="app__main"
        aria-labelledby="dashboard-title"
        aria-busy={connectionStatus === "loading"}
        tabIndex="-1"
      >
        <div className="app__intro">
          <div>
            <p className="label">OPERATE / REVIEW</p>
            <h2 id="dashboard-title" className="display">{workflowCopy.title}</h2>
          </div>
          <p className="app__intro-copy">
            {workflowCopy.description}
          </p>
        </div>
        {pageDataError && (
          <div className="app__connection-error" role="alert">
            <span>{pageDataError}</span>
            <button
              type="button"
              onClick={() => {
                refreshRuns();
                refreshJobs();
              }}
            >
              Retry connection
            </button>
          </div>
        )}

        <ActionPanel
          onJobStarted={handleJobStarted}
          onJobStopped={handleJobStopped}
          onTabChange={setWorkflowTab}
          runs={runs}
          jobs={jobs}
          brokerStatus={brokerStatus}
        />

        {isResearchTab && (
          <>
            <JobConsole
              jobs={jobs}
              loadStatus={dataStatus.jobs}
              selectedJobId={selectedJobId}
              onSelectJob={setSelectedJobId}
            />

            <div className="app__runs-row">
              <RunList
                runs={runs}
                activeRunId={activeRunId}
                compareRunId={compareRunId}
                onSelect={setActiveRunId}
                onCompare={setCompareRunId}
                onDelete={handleDeleteRun}
                loadStatus={dataStatus.runs}
              />
              <MetricsPanel
                metrics={runMetrics(activeRun)}
                title={activeRun?.kind === "optimize" ? "Out-of-sample metrics" : "Run metrics"}
                mlMetrics={ticker ? mlSummaryMetrics(activeRun, ticker) : null}
                mlTicker={ticker}
                trialsCount={optunaTrialsCount(activeRun)}
              />
            </div>

            <div className="app__instruments">
          {tickers.length > 1 && (
            <div className="app__ticker-select">
              <label className="label" htmlFor="channel-ticker">
                ML / Regime channel ticker
              </label>
              <select
                id="channel-ticker"
                value={ticker || ""}
                onChange={(e) => setTicker(e.target.value)}
              >
                {tickers.map((tk) => (
                  <option key={tk} value={tk}>
                    {tk}
                  </option>
                ))}
              </select>
            </div>
          )}

          <ChannelStrip
            label="EQUITY"
            color="var(--color-trace-amber)"
            series={equitySeries}
            ghostSeries={compareRun ? compareEquitySeries : null}
            ghostLabel={compareRun ? compareRunId : null}
            currentValueLabel={equitySeries.length ? formatUsd(equitySeries[equitySeries.length - 1].y) : "—"}
            emptyMessage="Run a backtest or Optuna sweep to populate this channel"
            tickFormat={formatUsdTick}
          />

          <ChannelStrip
            label="DRAWDOWN %"
            color="var(--color-trace-amber)"
            series={drawdownSeries}
            ghostSeries={compareRun ? compareDrawdownSeries : null}
            ghostLabel={compareRun ? compareRunId : null}
            thresholds={DRAWDOWN_THRESHOLDS}
            currentValueLabel={drawdownSeries.length ? `${drawdownSeries[drawdownSeries.length - 1].y.toFixed(1)}%` : "—"}
            emptyMessage="No equity curve yet"
            tickFormat={formatPctTick}
          />

          <ChannelStrip
            label={`PRICE: ACTUAL vs PREDICTED ${ticker ? `— ${ticker}` : ""}`}
            color="var(--color-text-primary)"
            series={actualSeries}
            overlaySeries={predictedSeries}
            overlayColor="var(--color-trace-cyan)"
            overlayLabel="predicted"
            showRailReading={false}
            currentValueLabel={
              ticker && activeRun?.ml_performance?.[ticker]?.price_series?.length
                ? formatUsd(
                    activeRun.ml_performance[ticker].price_series[
                      activeRun.ml_performance[ticker].price_series.length - 1
                    ].actual_price,
                  )
                : "—"
            }
            emptyMessage="No walk-forward price predictions yet"
            tickFormat={formatUsdTick}
          />

          <ChannelStrip
            label={`REGIME ${ticker ? `— ${ticker}` : ""}`}
            color="var(--color-trace-violet)"
            series={regimeSeries}
            showRailReading={false}
            currentValueLabel={
              ticker && activeRun?.regime?.[ticker]?.length
                ? activeRun.regime[ticker][activeRun.regime[ticker].length - 1].state_label
                : "—"
            }
            emptyMessage="Transition-matrix regime score — no data yet"
            tickFormat={formatScoreTick}
          />

            </div>
          </>
        )}

        {!isResearchTab && <LivePanel mode={workflowTab} />}
      </main>
    </div>
  );
}
