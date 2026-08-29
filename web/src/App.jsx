import { useCallback, useEffect, useMemo, useState } from "react";

import "./App.css";
import ActionPanel from "./components/bezel/ActionPanel.jsx";
import { BrokerStatus, WorkflowMenuButton } from "./components/bezel/InstrumentBezel.jsx";
import WorkflowDrawer from "./components/bezel/WorkflowDrawer.jsx";
import AssetProfileSwitch from "./components/asset-profile/AssetProfileSwitch.jsx";
import ChannelStrip from "./components/channel-strip/ChannelStrip.jsx";
import JobConsole from "./components/job-console/JobConsole.jsx";
import LivePanel from "./components/live-panel/LivePanel.jsx";
import MetricsPanel from "./components/metrics/MetricsPanel.jsx";
import ModelDecisionTape from "./components/model-tape/ModelDecisionTape.jsx";
import RunList from "./components/run-list/RunList.jsx";
import DockWorkspace from "./components/workspace/DockWorkspace.jsx";
import { useInterval } from "./hooks/useInterval.js";
import { api } from "./lib/api.js";
import { FALLBACK_ASSET_PROFILES, assetProfile, profileMap } from "./lib/assetProfiles.js";
import {
  drawdownPoints,
  equityPoints,
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

function initialAssetClass() {
  const requested = new URLSearchParams(window.location.search).get("asset");
  if (requested === "crypto" || requested === "equity") return requested;
  try {
    const settings = JSON.parse(window.localStorage.getItem("quant-dashboard.action-settings.v1"));
    return settings?.assetClass === "equity" ? "equity" : "crypto";
  } catch {
    return "crypto";
  }
}

const WORKFLOW_KEYS = new Set(["backtest", "optimize", "paper", "live"]);

function initialWorkflow() {
  const requested = new URLSearchParams(window.location.search).get("workflow");
  if (WORKFLOW_KEYS.has(requested)) return requested;
  try {
    const settings = JSON.parse(window.localStorage.getItem("quant-dashboard.action-settings.v1"));
    return WORKFLOW_KEYS.has(settings?.tab) ? settings.tab : "backtest";
  } catch {
    return "backtest";
  }
}

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
  const [workflowTab, setWorkflowTab] = useState(initialWorkflow);
  const [workflowMenuOpen, setWorkflowMenuOpen] = useState(false);
  const [controlDrawerOpen, setControlDrawerOpen] = useState(false);
  const [assetClass, setAssetClass] = useState(initialAssetClass);
  const [profiles, setProfiles] = useState(FALLBACK_ASSET_PROFILES);
  const selectedProfile = assetProfile(assetClass, profiles);

  const workflowCopy = {
    backtest: {
      label: "Backtest",
      title: "Run control & signal review",
      description: "Select a recorded run to inspect its risk rails, model performance, and regime traces.",
    },
    optimize: {
      label: "Optuna sweep",
      title: "Optimize the strategy",
      description: "Search across purged walk-forward folds, then inspect the untouched final test.",
    },
    paper: {
      label: "Paper trading",
      title: "Paper Trading",
      description: "Connect to paper TWS or Gateway and monitor the same strategy before risking capital.",
    },
    live: {
      label: "Live trading",
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

  async function refreshProfiles() {
    try {
      setProfiles(profileMap(await api.getProfiles()));
    } catch {
      setProfiles(FALLBACK_ASSET_PROFILES);
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
    refreshProfiles();
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("workflow", workflowTab);
    window.history.replaceState({}, "", url);
  }, [workflowTab]);

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
  const profileRuns = useMemo(
    () => runs.filter((run) => (run.asset_class || "crypto") === assetClass),
    [assetClass, runs],
  );

  useEffect(() => {
    window.localStorage.setItem("quant-dashboard.asset-profile.v1", assetClass);
    setActiveRunId((current) =>
      profileRuns.some((run) => run.run_id === current) ? current : profileRuns[0]?.run_id ?? null,
    );
    setCompareRunId(null);
  }, [assetClass, profileRuns]);

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
  const modelChart = ticker ? activeRun?.model_chart?.[ticker] || [] : [];
  const closeWorkflowMenu = useCallback(() => setWorkflowMenuOpen(false), []);
  const toggleWorkflowMenu = useCallback(() => setWorkflowMenuOpen((open) => !open), []);
  const selectWorkflow = useCallback((nextWorkflow) => {
    setWorkflowTab(nextWorkflow);
    setWorkflowMenuOpen(false);
    setControlDrawerOpen(false);
  }, []);

  const profileSwitch = (
    <AssetProfileSwitch
      compact
      value={assetClass}
      profiles={profiles}
      onChange={setAssetClass}
    />
  );
  const actionPanel = (
    <ActionPanel
      onJobStarted={handleJobStarted}
      onJobStopped={handleJobStopped}
      workflow={workflowTab}
      assetClass={assetClass}
      assetProfiles={profiles}
      onAssetClassChange={setAssetClass}
      runs={profileRuns}
      jobs={jobs}
      brokerStatus={brokerStatus}
    />
  );
  const researchPanels = [
    {
      id: "controls",
      title: `${workflowCopy.label} controls`,
      kind: "controls",
      reading: selectedProfile.short_label,
      defaultLayout: { x: 0, y: 0, w: 4, h: 6 },
      minW: 3,
      minH: 3,
      content: actionPanel,
    },
    {
      id: "runs",
      title: "Run library",
      kind: "runs",
      reading: `${profileRuns.length} RUNS`,
      defaultLayout: { x: 0, y: 6, w: 4, h: 3 },
      minW: 3,
      minH: 2,
      content: (
        <RunList
          runs={profileRuns}
          activeRunId={activeRunId}
          compareRunId={compareRunId}
          onSelect={setActiveRunId}
          onCompare={setCompareRunId}
          onDelete={handleDeleteRun}
          loadStatus={dataStatus.runs}
        />
      ),
    },
    {
      id: "metrics",
      title: "Performance / model score",
      kind: "metrics",
      reading: selectedProfile.scoring.short_label,
      defaultLayout: { x: 0, y: 9, w: 4, h: 3 },
      minW: 3,
      minH: 2,
      content: (
        <MetricsPanel
          metrics={runMetrics(activeRun)}
          title={activeRun?.kind === "optimize" ? "Out-of-sample metrics" : "Run metrics"}
          mlMetrics={ticker ? mlSummaryMetrics(activeRun, ticker) : null}
          mlTicker={ticker}
          trialsCount={optunaTrialsCount(activeRun)}
          objectiveMetric={activeRun?.objective_metric || selectedProfile.scoring.metric}
        />
      ),
    },
    {
      id: "model",
      title: "Model decision tape",
      kind: "model",
      reading: ticker || "NO SYMBOL",
      defaultLayout: { x: 4, y: 0, w: 8, h: 7 },
      minW: 4,
      minH: 4,
      content: (
        <div className="app__research-model">
          {tickers.length > 1 && (
            <div className="app__ticker-select">
              <label className="label" htmlFor="channel-ticker">Model symbol</label>
              <select id="channel-ticker" value={ticker || ""} onChange={(event) => setTicker(event.target.value)}>
                {tickers.map((symbol) => <option key={symbol} value={symbol}>{symbol}</option>)}
              </select>
            </div>
          )}
          <ModelDecisionTape
            points={modelChart}
            ticker={ticker || "—"}
            model={{
              ...activeRun?.model_chart_meta,
              entry_threshold: modelChart.at(-1)?.entry_threshold,
              protective_orders_submitted: false,
            }}
            assetClass={assetClass}
          />
        </div>
      ),
    },
    {
      id: "equity",
      title: "Equity curve",
      kind: "channel",
      reading: equitySeries.length ? formatUsd(equitySeries.at(-1).y) : "—",
      defaultLayout: { x: 4, y: 7, w: 4, h: 2 },
      minW: 3,
      minH: 2,
      content: (
        <ChannelStrip
          label="EQUITY"
          color="var(--color-trace-amber)"
          series={equitySeries}
          ghostSeries={compareRun ? compareEquitySeries : null}
          ghostLabel={compareRun ? compareRunId : null}
          currentValueLabel={equitySeries.length ? formatUsd(equitySeries.at(-1).y) : "—"}
          emptyMessage="Run a backtest or Optuna sweep to populate this channel"
          tickFormat={formatUsdTick}
        />
      ),
    },
    {
      id: "drawdown",
      title: "Drawdown / risk rails",
      kind: "channel",
      reading: drawdownSeries.length ? `${drawdownSeries.at(-1).y.toFixed(1)}%` : "—",
      defaultLayout: { x: 8, y: 7, w: 4, h: 2 },
      minW: 3,
      minH: 2,
      content: (
        <ChannelStrip
          label="DRAWDOWN %"
          color="var(--color-trace-amber)"
          series={drawdownSeries}
          ghostSeries={compareRun ? compareDrawdownSeries : null}
          ghostLabel={compareRun ? compareRunId : null}
          thresholds={DRAWDOWN_THRESHOLDS}
          currentValueLabel={drawdownSeries.length ? `${drawdownSeries.at(-1).y.toFixed(1)}%` : "—"}
          emptyMessage="No equity curve yet"
          tickFormat={formatPctTick}
        />
      ),
    },
    {
      id: "jobs",
      title: "Job console",
      kind: "jobs",
      reading: `${runningJobCount} RUNNING`,
      defaultLayout: { x: 4, y: 9, w: 8, h: 3 },
      minW: 4,
      minH: 2,
      content: (
        <JobConsole
          jobs={jobs}
          loadStatus={dataStatus.jobs}
          selectedJobId={selectedJobId}
          onSelectJob={setSelectedJobId}
        />
      ),
    },
  ];
  const workspaceNavigation = (
    <WorkflowMenuButton
      menuOpen={workflowMenuOpen}
      onMenuToggle={toggleWorkflowMenu}
    />
  );
  const workspaceBrokerStatus = (
    <BrokerStatus
      runningJobCount={runningJobCount}
      brokerStatus={brokerStatus}
    />
  );

  return (
    <div className={`app ${workflowTab === "live" ? "is-live" : ""}`}>
      <a className="skip-link" href="#dashboard-main">Skip to dashboard</a>
      <WorkflowDrawer
        open={workflowMenuOpen}
        activeWorkflow={workflowTab}
        runningJobCount={runningJobCount}
        onClose={closeWorkflowMenu}
        onSelect={selectWorkflow}
      />
      <main id="dashboard-main" className="app__main" aria-busy={connectionStatus === "loading"} tabIndex="-1">
        {pageDataError && (
          <div className="app__connection-error" role="alert">
            <span>{pageDataError}</span>
            <button type="button" onClick={() => { refreshRuns(); refreshJobs(); }}>Retry connection</button>
          </div>
        )}
        {isResearchTab ? (
          <DockWorkspace
            key={`${workflowTab}-${assetClass}`}
            workspaceId={`${workflowTab}-${assetClass}`}
            title={`${workflowCopy.label} · ${selectedProfile.short_label}`}
            subtitle={workflowCopy.description}
            toolbarNavigation={workspaceNavigation}
            toolbarLead={profileSwitch}
            toolbarStatus={workspaceBrokerStatus}
            panels={researchPanels}
          />
        ) : (
          <LivePanel
            mode={workflowTab}
            assetClass={assetClass}
            profile={selectedProfile}
            brokerStatus={brokerStatus}
            toolbarNavigation={workspaceNavigation}
            toolbarLead={profileSwitch}
            toolbarActions={(
              <button type="button" onClick={() => setControlDrawerOpen(true)}>Session controls</button>
            )}
            toolbarStatus={workspaceBrokerStatus}
          />
        )}
      </main>
      {!isResearchTab && (
        <ControlDrawer open={controlDrawerOpen} title={`${workflowCopy.label} controls`} onClose={() => setControlDrawerOpen(false)}>
          {actionPanel}
        </ControlDrawer>
      )}
    </div>
  );
}

function ControlDrawer({ open, title, onClose, children }) {
  useEffect(() => {
    if (!open) return undefined;
    function handleKeyDown(event) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  return (
    <aside
      className={`app__control-drawer ${open ? "is-open" : ""}`}
      aria-hidden={!open}
      inert={open ? undefined : ""}
    >
      <header>
        <h2>{title}</h2>
        <button type="button" onClick={onClose} aria-label={`Close ${title}`}>×</button>
      </header>
      <div className="app__control-drawer-body">{children}</div>
    </aside>
  );
}
