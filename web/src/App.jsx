import { useCallback, useEffect, useMemo, useState } from "react";

import "./App.css";
import ActionPanel from "./components/bezel/ActionPanel.jsx";
import { BrokerStatus, WorkflowMenuButton } from "./components/bezel/InstrumentBezel.jsx";
import WorkflowDrawer from "./components/bezel/WorkflowDrawer.jsx";
import AssetProfileSwitch from "./components/asset-profile/AssetProfileSwitch.jsx";
import LivePanel from "./components/live-panel/LivePanel.jsx";
import ResearchHub from "./components/research-hub/ResearchHub.jsx";
import { useInterval } from "./hooks/useInterval.js";
import { api } from "./lib/api.js";
import { FALLBACK_ASSET_PROFILES, assetProfile, profileMap } from "./lib/assetProfiles.js";
import { runTickers } from "./lib/deriveChannels.js";
import { activeRootJobCount, isJobActive } from "./lib/jobs.js";
import { applyDashboardTheme, initialDashboardTheme } from "./lib/theme.js";

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
  const [apiHealth, setApiHealth] = useState({ status: "loading", job_registry: null });
  const [brokerStatus, setBrokerStatus] = useState({ status: "loading" });
  const [activeRunId, setActiveRunId] = useState(null);
  const [compareRunId, setCompareRunId] = useState(null);
  const [activeRun, setActiveRun] = useState(null);
  const [compareRun, setCompareRun] = useState(null);
  const [ticker, setTicker] = useState(null);
  const [workflowTab, setWorkflowTab] = useState(initialWorkflow);
  const [workflowMenuOpen, setWorkflowMenuOpen] = useState(false);
  const [controlDrawerOpen, setControlDrawerOpen] = useState(false);
  const [assetClass, setAssetClass] = useState(initialAssetClass);
  const [profiles, setProfiles] = useState(FALLBACK_ASSET_PROFILES);
  const [theme, setTheme] = useState(initialDashboardTheme);
  const selectedProfile = assetProfile(assetClass, profiles);

  useEffect(() => {
    applyDashboardTheme(theme);
  }, [theme]);

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
        sameCollection(current, list, [
          "id",
          "status",
          "finished_at",
          "cancel_requested_at",
          "failure_reason",
          "return_code",
          "parent_job_id",
        ]) ? current : list,
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

  async function refreshApiHealth() {
    try {
      setApiHealth(await api.health());
      setDataErrors((current) => ({ ...current, health: null }));
    } catch (error) {
      setApiHealth({ status: "unknown", job_registry: null, error: error.message });
      setDataErrors((current) => ({
        ...current,
        health: `API health unavailable: ${error.message}. Durable job state is unknown.`,
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
    refreshApiHealth();
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
  useInterval(refreshApiHealth, 5000);
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
  const workflowRuns = useMemo(
    () => profileRuns.filter((run) => run.kind === workflowTab),
    [profileRuns, workflowTab],
  );

  useEffect(() => {
    window.localStorage.setItem("quant-dashboard.asset-profile.v1", assetClass);
    setActiveRunId((current) =>
      workflowRuns.some((run) => run.run_id === current) ? current : workflowRuns[0]?.run_id ?? null,
    );
    setCompareRunId(null);
  }, [assetClass, workflowRuns]);

  useEffect(() => {
    if (tickers.length && !tickers.includes(ticker)) {
      setTicker(tickers[0]);
    }
  }, [tickers, ticker]);

  function handleJobStarted(job) {
    setJobs((prev) => [job, ...prev]);
    if (job.run_id) {
      // Backtest/optimize jobs write a run artifact once they finish; keep
      // polling the run list so the new run appears without a manual refresh.
      const poll = setInterval(async () => {
        const detail = await api.getJob(job.id).catch(() => null);
        if (detail) {
          setJobs((current) => current.map((item) => (item.id === detail.id ? detail : item)));
        }
        if (detail && !isJobActive(detail)) {
          clearInterval(poll);
          if (detail.status === "completed") {
            await refreshRuns();
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
  const runningJobCount = activeRootJobCount(jobs);
  const connectionStatus =
    dataStatus.runs === "error" || dataStatus.jobs === "error" || apiHealth.status === "unknown"
      ? "unknown"
      : dataStatus.runs === "loading" || dataStatus.jobs === "loading" || apiHealth.status === "loading"
        ? "loading"
        : "ready";
  const dataError = Object.values(dataErrors).filter(Boolean).join(" ");
  const pageDataError = isResearchTab ? dataError : null;

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
      apiHealth={apiHealth}
    />
  );

  return (
    <div className={`app ${isResearchTab ? "is-research" : ""} ${workflowTab === "live" ? "is-live" : ""}`}>
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
            <button type="button" onClick={() => { refreshRuns(); refreshJobs(); refreshApiHealth(); }}>Retry connection</button>
          </div>
        )}
        {isResearchTab ? (
          <ResearchHub
            key={workflowTab}
            workflow={workflowTab}
            profile={selectedProfile}
            assetProfiles={profiles}
            assetClass={assetClass}
            onAssetClassChange={setAssetClass}
            runs={workflowRuns}
            allRuns={profileRuns}
            activeRunId={activeRunId}
            compareRunId={compareRunId}
            activeRun={activeRun}
            compareRun={compareRun}
            onSelectRun={setActiveRunId}
            onCompareRun={setCompareRunId}
            onDeleteRun={handleDeleteRun}
            runLoadStatus={dataStatus.runs}
            jobs={jobs}
            onJobStarted={handleJobStarted}
            onJobStopped={handleJobStopped}
            brokerStatus={brokerStatus}
            toolbarNavigation={workspaceNavigation}
            toolbarLead={profileSwitch}
            toolbarStatus={workspaceBrokerStatus}
            theme={theme}
            onThemeChange={setTheme}
            ticker={ticker}
            onTickerChange={setTicker}
          />
        ) : (
          <LivePanel
            mode={workflowTab}
            assetClass={assetClass}
            profile={selectedProfile}
            brokerStatus={brokerStatus}
            apiHealth={apiHealth}
            jobs={jobs}
            toolbarNavigation={workspaceNavigation}
            toolbarLead={profileSwitch}
            toolbarActions={(
              <button type="button" onClick={() => setControlDrawerOpen(true)}>Session controls</button>
            )}
            toolbarStatus={workspaceBrokerStatus}
            theme={theme}
            onThemeChange={setTheme}
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
