import { cloneElement, isValidElement, useEffect, useId, useRef, useState } from "react";

import { api } from "../../lib/api.js";
import { formatTime } from "../../lib/format.js";
import FeaturePanel, { DEFAULT_FEATURES, DEFAULT_SEARCH_MODES } from "../features/FeaturePanel.jsx";
import RiskPanel, { DEFAULT_RISK, toRiskOverrides } from "../features/RiskPanel.jsx";
import "./action-panel.css";

const LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS DEPLOYS REAL CAPITAL";
const PAPER_PORTS = new Set([7497, 4002]);
const SETTINGS_STORAGE_KEY = "quant-dashboard.action-settings.v1";

// Mirrors strategies/risk.py's default rails. Paper/live can load a params
// JSON through the native file picker; otherwise these defaults apply.
const TABS = [
  { key: "backtest", label: "Run Backtest" },
  { key: "optimize", label: "Start Optuna Sweep" },
  { key: "paper", label: "Paper Trading" },
  { key: "live", label: "Live Trading", danger: true },
];

function readPersistedSettings() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(SETTINGS_STORAGE_KEY));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

// api/schemas.py's BacktestJobRequest/OptimizeJobRequest both default an
// omitted csv to quant/data/sample_bars.csv (synthetic data). Leaving this
// field blank in the dashboard used to silently send no csv at all, so every
// dashboard-triggered run traded sample data even when real IBKR bars had
// been fetched. Fall back to the real per-asset-class file instead.
function defaultCsvPath(assetClassValue) {
  return assetClassValue === "equity"
    ? "quant/data/equity_bars.csv"
    : "quant/data/ibkr_bars.csv";
}

function parseTickers(raw) {
  const list = raw
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  return list.length ? list : undefined;
}

function formatParamValue(v) {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(4);
  return String(v);
}

export default function ActionPanel({
  onJobStarted,
  onJobStopped,
  onTabChange,
  runs = [],
  jobs = [],
  brokerStatus = {},
}) {
  const [persisted] = useState(readPersistedSettings);
  const [tab, setTab] = useState(persisted.tab || "backtest");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [showAdvanced, setShowAdvanced] = useState(Boolean(persisted.showAdvanced));
  const brokerCash = brokerStatus.account?.cash;
  const accountCashLabel = Number.isFinite(brokerCash)
    ? new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: brokerStatus.account?.currency || "USD",
        maximumFractionDigits: 2,
      }).format(brokerCash)
    : brokerStatus.status === "connected"
      ? "Waiting for account data…"
      : "IBKR not connected";

  const [tickers, setTickers] = useState(persisted.tickers ?? "BTC,ETH,SOL,XRP,DOGE");
  const [assetClass, setAssetClass] = useState(persisted.assetClass || "crypto");
  const [csvPath, setCsvPath] = useState(persisted.csvPath ?? "");
  const [cash, setCash] = useState(persisted.cash ?? 5000);
  const [trials, setTrials] = useState(persisted.trials ?? 40);
  const [stopMode, setStopMode] = useState(persisted.stopMode || "trials"); // "trials" | "score"
  const [targetScore, setTargetScore] = useState(persisted.targetScore ?? 1.5);
  const [host, setHost] = useState(persisted.host ?? "127.0.0.1");
  const [port, setPort] = useState(persisted.port ?? 7497);
  const [livePort, setLivePort] = useState(persisted.livePort ?? "");
  const [clientId, setClientId] = useState(persisted.clientId ?? 1);
  const [accountId, setAccountId] = useState(persisted.accountId ?? "");
  const [tradingParams, setTradingParams] = useState(persisted.tradingParams ?? null);
  const [tradingParamsName, setTradingParamsName] = useState(
    persisted.tradingParamsName ?? "",
  );
  const tradingParamsInput = useRef(null);
  const [primaryExchange, setPrimaryExchange] = useState(persisted.primaryExchange ?? "");
  const [allowShorts, setAllowShorts] = useState(Boolean(persisted.allowShorts));

  const [features, setFeatures] = useState({
    ...DEFAULT_FEATURES,
    ...(persisted.features || {}),
  });
  const [searchModes, setSearchModes] = useState({
    ...DEFAULT_SEARCH_MODES,
    ...(persisted.searchModes || {}),
  });
  const [risk, setRisk] = useState({ ...DEFAULT_RISK, ...(persisted.risk || {}) });
  const [dataFetchMode, setDataFetchMode] = useState(
    persisted.dataFetchMode ?? (persisted.fetchMissing ? "missing" : "none"),
  );
  const [ibkrPort, setIbkrPort] = useState(persisted.ibkrPort ?? 7497);
  const [ibkrYears, setIbkrYears] = useState(persisted.ibkrYears ?? 5);
  const [ibkrBarHours, setIbkrBarHours] = useState(persisted.ibkrBarHours ?? 4);

  const [sourceRunId, setSourceRunId] = useState(persisted.sourceRunId ?? "");
  const [loadedParams, setLoadedParams] = useState(persisted.loadedParams ?? null);
  const [loadingParams, setLoadingParams] = useState(false);
  const [resumeRunId, setResumeRunId] = useState(persisted.resumeRunId ?? "");
  const optimizeRuns = runs.filter((r) => r.kind === "optimize");
  const paperJob = jobs.find((job) => job.kind === "paper" && job.status === "running");

  useEffect(() => {
    onTabChange?.(tab);
  }, [onTabChange, tab]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SETTINGS_STORAGE_KEY,
        JSON.stringify({
          tab,
          showAdvanced,
          tickers,
          assetClass,
          csvPath,
          cash,
          trials,
          stopMode,
          targetScore,
          host,
          port,
          livePort,
          clientId,
          accountId,
          tradingParams,
          tradingParamsName,
          primaryExchange,
          allowShorts,
          features,
          searchModes,
          risk,
          dataFetchMode,
          ibkrPort,
          ibkrYears,
          ibkrBarHours,
          sourceRunId,
          loadedParams,
          resumeRunId,
        }),
      );
    } catch {
      // Storage may be disabled or full; the controls remain usable in-memory.
    }
  }, [
    tab,
    showAdvanced,
    tickers,
    assetClass,
    csvPath,
    cash,
    trials,
    stopMode,
    targetScore,
    host,
    port,
    livePort,
    clientId,
    accountId,
    tradingParams,
    tradingParamsName,
    primaryExchange,
    allowShorts,
    features,
    searchModes,
    risk,
    dataFetchMode,
    ibkrPort,
    ibkrYears,
    ibkrBarHours,
    sourceRunId,
    loadedParams,
    resumeRunId,
  ]);

  function selectTab(nextTab) {
    setTab(nextTab);
    setError(null);
  }

  function handleTabKeyDown(event, currentIndex) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let nextIndex = currentIndex;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + TABS.length) % TABS.length;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % TABS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = TABS.length - 1;
    const nextTab = TABS[nextIndex].key;
    selectTab(nextTab);
    document.getElementById(`workflow-tab-${nextTab}`)?.focus();
  }

  async function handleTradingParamsFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError(null);
    try {
      const parsed = JSON.parse(await file.text());
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("the file must contain a JSON object");
      }
      setTradingParams(parsed);
      setTradingParamsName(file.name);
    } catch (err) {
      setTradingParams(null);
      setTradingParamsName("");
      event.target.value = "";
      setError(`Invalid strategy params file: ${err.message}`);
    }
  }

  function clearTradingParams() {
    setTradingParams(null);
    setTradingParamsName("");
    if (tradingParamsInput.current) tradingParamsInput.current.value = "";
  }

  // Resuming reattaches to a prior sweep's Optuna study (see optimize.py's
  // --resume-run-id) and keeps adding trials to it, so the search space MUST
  // match the original run -- auto-fill tickers/asset class/cash from it to
  // remove the most common way to accidentally mismatch it.
  async function handleSelectResumeRun(runId) {
    setResumeRunId(runId);
    if (!runId) return;
    setError(null);
    try {
      const run = await api.getRun(runId);
      setTickers((run.tickers || []).join(","));
      setAssetClass(run.asset_class || "crypto");
      setCash(run.starting_cash ?? 5000);
    } catch (err) {
      setError(`Failed to load run ${runId} for resume: ${err.message}`);
    }
  }

  // Loads an Optuna run's tuned hyperparameters (n_lags, horizon,
  // entry_threshold, atr_period, huber_alpha, ... -- study.best_params) as
  // the BASE params for a backtest job, so you can re-run a backtest with
  // exactly what a sweep achieved. n_lags/cross_asset_lags/spread_lags are
  // ALSO synced into the FeaturePanel's manual value here -- otherwise those
  // three fields are always sent as real numbers (never null) for a backtest,
  // and would silently clobber the loaded value back to FeaturePanel's
  // hardcoded defaults (see structuralPayload/_write_params_file below).
  async function handleSelectSourceRun(runId) {
    setSourceRunId(runId);
    if (!runId) {
      setLoadedParams(null);
      return;
    }
    setLoadingParams(true);
    setError(null);
    try {
      const run = await api.getRun(runId);
      const params = run.best_params || {};
      setLoadedParams(params);
      setFeatures((prev) => ({
        ...prev,
        ...(params.n_lags != null && { n_lags: params.n_lags }),
        ...(params.cross_asset_lags != null && { cross_asset_lags: params.cross_asset_lags }),
        ...(params.spread_lags != null && { spread_lags: params.spread_lags }),
      }));
    } catch (err) {
      setError(`Failed to load params from ${runId}: ${err.message}`);
      setLoadedParams(null);
    } finally {
      setLoadingParams(false);
    }
  }

  // For an Optuna sweep, a field left in "optuna" mode must be sent as null
  // so api/schemas.py's FeatureConfig.as_overrides() omits it entirely --
  // that's what lets optimize.py's trial.suggest_int() run for that field
  // instead of being clobbered by a fixed structural override. Backtest has
  // no Optuna trials to search, so it always sends the manual value.
  function effectiveFeatures() {
    if (tab !== "optimize") return features;
    const optunaFields = Object.entries(searchModes)
      .filter(([, mode]) => mode === "optuna")
      .map(([key]) => key);
    return { ...features, ...Object.fromEntries(optunaFields.map((key) => [key, null])) };
  }

  function structuralPayload() {
    return {
      features: effectiveFeatures(),
      risk: toRiskOverrides(risk),
      ibkr: {
        fetch_missing: dataFetchMode === "missing",
        replace_bars: dataFetchMode === "replace",
        ibkr_host: host,
        ibkr_port: Number(ibkrPort),
        ibkr_client_id: Number(clientId),
        ibkr_years: Number(ibkrYears),
        ibkr_bar_hours: dataFetchMode === "replace" ? Number(ibkrBarHours) : null,
      },
    };
  }

  async function submit() {
    setPending(true);
    setError(null);
    try {
      let job;
      if (tab === "backtest") {
        job = await api.startBacktest({
          csv: csvPath.trim() || defaultCsvPath(assetClass),
          tickers: parseTickers(tickers),
          asset_class: assetClass,
          cash: Number(cash),
          params: loadedParams,
          ...structuralPayload(),
        });
      } else if (tab === "optimize") {
        job = await api.startOptimize({
          csv: csvPath.trim() || defaultCsvPath(assetClass),
          tickers: parseTickers(tickers),
          asset_class: assetClass,
          cash: Number(cash),
          trials: stopMode === "score" ? (trials ? Number(trials) : null) : Number(trials),
          score: stopMode === "score" ? Number(targetScore) : null,
          resume_run_id: resumeRunId || null,
          ...structuralPayload(),
        });
      } else if (tab === "paper") {
        await api.configureBroker({
          host,
          port: Number(port),
          account_id: accountId || "",
          mode: "paper",
        });
        job = await api.startPaper({
          tickers: parseTickers(tickers) || ["BTC"],
          asset_class: assetClass,
          primary_exchange: primaryExchange,
          allow_shorts: allowShorts,
          host,
          port: Number(port),
          client_id: Number(clientId),
          account_id: accountId || null,
          params: { ...(tradingParams || {}), ...toRiskOverrides(risk) },
        });
      } else if (tab === "live") {
        await api.configureBroker({
          host,
          port: Number(livePort),
          account_id: accountId || "",
          mode: "live",
        });
        job = await api.startLive({
          tickers: parseTickers(tickers) || ["BTC"],
          asset_class: assetClass,
          primary_exchange: primaryExchange,
          allow_shorts: allowShorts,
          host,
          port: Number(livePort),
          client_id: Number(clientId),
          account_id: accountId || null,
          cash: Number(cash),
          params: { ...(tradingParams || {}), ...toRiskOverrides(risk) },
          confirm: LIVE_CONFIRM_PHRASE,
        });
      }
      onJobStarted?.(job);
    } catch (err) {
      setError(err.message);
    } finally {
      setPending(false);
    }
  }

  async function stopPaperTrading() {
    if (!paperJob || !window.confirm("Stop paper trading and return to the current page?")) return;
    setPending(true);
    setError(null);
    try {
      const stoppedJob = await api.cancelJob(paperJob.id);
      onJobStopped?.(stoppedJob);
    } catch (err) {
      setError(`Could not stop paper trading: ${err.message}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="action-panel" aria-labelledby="action-panel-title">
      <h2 id="action-panel-title" className="sr-only">
        Run controls
      </h2>
      <div className="action-panel__tabs" role="tablist" aria-label="Trading workflow">
        {TABS.map((t, index) => (
          <button
            key={t.key}
            id={`workflow-tab-${t.key}`}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            aria-controls={`workflow-panel-${t.key}`}
            tabIndex={tab === t.key ? 0 : -1}
            className={`action-panel__tab ${tab === t.key ? "is-active" : ""} ${t.danger ? "is-danger" : ""}`}
            onClick={() => selectTab(t.key)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        id={`workflow-panel-${tab}`}
        className="action-panel__form"
        role="tabpanel"
        aria-labelledby={`workflow-tab-${tab}`}
      >
        {(tab === "backtest" || tab === "optimize") && (
          <>
            <div className="action-panel__fields action-panel__fields--research">
              <Field label="Tickers">
                <input value={tickers} onChange={(e) => setTickers(e.target.value)} />
              </Field>
              <Field label="Asset class">
                <select value={assetClass} onChange={(e) => setAssetClass(e.target.value)}>
                  <option value="crypto">Crypto</option>
                  <option value="equity">Equity</option>
                </select>
              </Field>
              <Field label="Cash">
                <input type="number" value={cash} onChange={(e) => setCash(e.target.value)} />
              </Field>
              <Field label="Data CSV" wide>
                <input
                  value={csvPath}
                  onChange={(e) => setCsvPath(e.target.value)}
                  placeholder={`${defaultCsvPath(assetClass)} (default)`}
                />
              </Field>

              {tab === "backtest" && optimizeRuns.length > 0 && (
                <Field label="Load hyperparameters from Optuna run">
                  <select
                    value={sourceRunId}
                    onChange={(e) => handleSelectSourceRun(e.target.value)}
                    disabled={loadingParams}
                  >
                    <option value="">— none (defaults) —</option>
                    {optimizeRuns.map((r) => (
                      <option key={r.run_id} value={r.run_id}>
                        {r.run_id} ({formatTime(r.finished_at)})
                      </option>
                    ))}
                  </select>
                </Field>
              )}

              {tab === "optimize" && (
                <>
                  {optimizeRuns.length > 0 && (
                    <Field label="Resume Optuna run (optional)">
                      <select value={resumeRunId} onChange={(e) => handleSelectResumeRun(e.target.value)}>
                        <option value="">— start fresh —</option>
                        {optimizeRuns.map((r) => (
                          <option key={r.run_id} value={r.run_id}>
                            {r.run_id} ({formatTime(r.finished_at)})
                          </option>
                        ))}
                      </select>
                    </Field>
                  )}
                  <Field label="Stop condition">
                    <select value={stopMode} onChange={(e) => setStopMode(e.target.value)}>
                      <option value="trials">Fixed trial count</option>
                      <option value="score">Run until score reached</option>
                    </select>
                  </Field>
                  {stopMode === "trials" ? (
                    <Field label="Trials">
                      <input type="number" value={trials} onChange={(e) => setTrials(e.target.value)} />
                    </Field>
                  ) : (
                    <>
                      <Field label="Target score (Sortino-like)">
                        <input
                          type="number"
                          step={0.1}
                          value={targetScore}
                          onChange={(e) => setTargetScore(e.target.value)}
                        />
                      </Field>
                      <Field label="Safety cap trials (optional)">
                        <input
                          type="number"
                          placeholder="uncapped"
                          value={trials}
                          onChange={(e) => setTrials(e.target.value)}
                        />
                      </Field>
                    </>
                  )}
                </>
              )}
            </div>

            <div className="action-panel__actions">
              <button className="button-primary" disabled={pending} onClick={submit}>
                {pending ? "Starting…" : tab === "backtest" ? "Run Backtest" : "Start Sweep"}
              </button>

              <button
                type="button"
                className="action-panel__advanced-toggle label"
                aria-expanded={showAdvanced}
                aria-controls="advanced-run-settings"
                onClick={() => setShowAdvanced((v) => !v)}
              >
                {showAdvanced ? "Hide" : "Show"} feature / risk / data settings
              </button>
            </div>

            {tab === "backtest" && loadedParams && (
              <div className="action-panel__loaded-params label">
                Loaded {Object.keys(loadedParams).length} param(s) from {sourceRunId}:{" "}
                {Object.entries(loadedParams)
                  .map(([k, v]) => `${k}=${formatParamValue(v)}`)
                  .join(", ")}
              </div>
            )}

            {tab === "optimize" && resumeRunId && (
              <div className="action-panel__loaded-params label">
                Resuming {resumeRunId} — tickers/asset class/cash auto-filled from that run.
                Keep Feature/Risk settings identical too, or the accumulated study mixes
                incompatible search spaces.
              </div>
            )}

            {showAdvanced && (
              <div className="action-panel__advanced" id="advanced-run-settings">
                <FeaturePanel
                  value={features}
                  onChange={setFeatures}
                  searchModes={searchModes}
                  onSearchModesChange={setSearchModes}
                  allowOptunaSearch={tab === "optimize"}
                />
                <RiskPanel value={risk} onChange={setRisk} />
                <div className="action-panel__ibkr">
                  <div className="action-panel__ibkr-mode">
                    <Field label="Historical data action">
                      <select value={dataFetchMode} onChange={(e) => setDataFetchMode(e.target.value)}>
                        <option value="none">Use current CSV</option>
                        <option value="missing">Add missing tickers · keep frequency</option>
                        <option value="replace">Replace all bars · change frequency</option>
                      </select>
                    </Field>
                    <div className="action-panel__ibkr-copy-block">
                      <span className="label">Frequency contract</span>
                      <p className="action-panel__ibkr-copy">
                        {dataFetchMode === "missing"
                          ? "Only absent tickers are fetched. Their bars inherit the current CSV frequency."
                          : dataFetchMode === "replace"
                            ? "The requested universe replaces the CSV completely at the selected frequency."
                            : "No IBKR request will be made; the selected CSV is used unchanged."}
                      </p>
                    </div>
                  </div>
                  {dataFetchMode !== "none" && (
                    <div className="action-panel__ibkr-fields">
                      <Field label="IBKR host">
                        <input value={host} onChange={(e) => setHost(e.target.value)} />
                      </Field>
                      <Field label="IBKR port">
                        <input type="number" value={ibkrPort} onChange={(e) => setIbkrPort(e.target.value)} />
                      </Field>
                      <Field label="Years of history">
                        <input type="number" value={ibkrYears} onChange={(e) => setIbkrYears(e.target.value)} />
                      </Field>
                      {dataFetchMode === "replace" && (
                        <Field label="Replacement bar frequency">
                          <select value={ibkrBarHours} onChange={(e) => setIbkrBarHours(e.target.value)}>
                            <option value={1}>1 hour</option>
                            <option value={2}>2 hours</option>
                            <option value={3}>3 hours</option>
                            <option value={4}>4 hours (default)</option>
                            <option value={8}>8 hours</option>
                            <option value={12}>12 hours</option>
                            <option value={24}>24 hours (1 day)</option>
                          </select>
                        </Field>
                      )}
                    </div>
                  )}
                  {dataFetchMode === "replace" && (
                    <div className="action-panel__loaded-params">
                      Replacement is destructive to the selected CSV: existing tickers and
                      their old-frequency bars are removed. The strategy's regime/risk windows
                      remain day-denominated and are not rescaled.
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}

        {tab === "paper" && (
          <>
            <RiskPanel value={risk} onChange={setRisk} />
            {!paperJob && <div className="action-panel__fields action-panel__fields--trading">
              <Field label="Asset class">
                <select value={assetClass} onChange={(e) => setAssetClass(e.target.value)}>
                  <option value="crypto">Crypto</option>
                  <option value="equity">Equity / ETF</option>
                </select>
              </Field>
              <Field label="Tickers">
                <input value={tickers} onChange={(e) => setTickers(e.target.value)} />
              </Field>
              {assetClass === "equity" && (
                <>
                  <Field label="Primary exchange (optional)">
                    <input
                      value={primaryExchange}
                      onChange={(e) => setPrimaryExchange(e.target.value)}
                      placeholder="SMART auto-qualification"
                    />
                  </Field>
                  <Field label="Allow short positions">
                    <input
                      type="checkbox"
                      checked={allowShorts}
                      onChange={(e) => setAllowShorts(e.target.checked)}
                    />
                  </Field>
                </>
              )}
              <Field label="Account cash">
                <div className="action-panel__account-cash" role="status">
                  <span className="num action-panel__account-cash-value">{accountCashLabel}</span>
                </div>
              </Field>
              <Field label="Strategy" wide>
                <StrategyParamsPicker
                  inputRef={tradingParamsInput}
                  fileName={tradingParamsName}
                  onChange={handleTradingParamsFile}
                  onClear={clearTradingParams}
                />
              </Field>
            </div>}
            {!paperJob && <details className="action-panel__trading-advanced">
              <summary className="label">Advanced</summary>
              <div className="action-panel__fields action-panel__fields--trading-advanced">
                <Field label="Host">
                  <input value={host} onChange={(e) => setHost(e.target.value)} />
                </Field>
                <Field label="Port">
                  <input type="number" value={port} onChange={(e) => setPort(e.target.value)} />
                </Field>
                <Field label="Client ID">
                  <input type="number" value={clientId} onChange={(e) => setClientId(e.target.value)} />
                </Field>
                <Field label="Paper account ID">
                  <input
                    value={accountId}
                    onChange={(e) => setAccountId(e.target.value)}
                    placeholder="DU1234567"
                  />
                </Field>
              </div>
            </details>}
            <div className="action-panel__actions">
              <button
                className={paperJob ? "button-danger" : "button-primary"}
                disabled={pending}
                onClick={paperJob ? stopPaperTrading : submit}
              >
                {pending
                  ? (paperJob ? "Stopping…" : "Starting…")
                  : (paperJob ? "Stop Paper Trading" : "Start Paper Trading")}
              </button>
            </div>
          </>
        )}

        {tab === "live" && (
          <>
            <RiskPanel value={risk} onChange={setRisk} />
            <div className="action-panel__fields action-panel__fields--trading">
              <Field label="Asset class">
                <select value={assetClass} onChange={(e) => setAssetClass(e.target.value)}>
                  <option value="crypto">Crypto</option>
                  <option value="equity">Equity / ETF</option>
                </select>
              </Field>
              <Field label="Tickers">
                <input value={tickers} onChange={(e) => setTickers(e.target.value)} />
              </Field>
              {assetClass === "equity" && (
                <>
                  <Field label="Primary exchange (optional)">
                    <input
                      value={primaryExchange}
                      onChange={(e) => setPrimaryExchange(e.target.value)}
                      placeholder="SMART auto-qualification"
                    />
                  </Field>
                  <Field label="Allow short positions">
                    <input
                      type="checkbox"
                      checked={allowShorts}
                      onChange={(e) => setAllowShorts(e.target.checked)}
                    />
                  </Field>
                </>
              )}
              <Field label="Strategy" wide>
                <StrategyParamsPicker
                  inputRef={tradingParamsInput}
                  fileName={tradingParamsName}
                  onChange={handleTradingParamsFile}
                  onClear={clearTradingParams}
                />
              </Field>
            </div>
            <details className="action-panel__trading-advanced">
              <summary className="label">Advanced</summary>
              <div className="action-panel__fields action-panel__fields--trading-advanced">
                <Field label="Host">
                  <input value={host} onChange={(e) => setHost(e.target.value)} />
                </Field>
                <Field label="Live port (not 7497)">
                  <input
                    type="number"
                    placeholder="7496"
                    value={livePort}
                    onChange={(e) => setLivePort(e.target.value)}
                  />
                </Field>
                <Field label="Client ID">
                  <input type="number" value={clientId} onChange={(e) => setClientId(e.target.value)} />
                </Field>
                <Field label="Live account ID">
                  <input value={accountId} onChange={(e) => setAccountId(e.target.value)} />
                </Field>
              </div>
            </details>
            <div className="action-panel__actions">
              <button className="button-danger" disabled={pending || !Number(livePort) || PAPER_PORTS.has(Number(livePort))} onClick={submit}>
                {pending ? "Arming…" : "Start Live Trading"}
              </button>
            </div>
          </>
        )}
      </div>

      {error && (
        <div className="action-panel__error" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

function StrategyParamsPicker({ inputRef, fileName, onChange, onClear }) {
  return (
    <div className="action-panel__file-control">
      <input
        ref={inputRef}
        className="action-panel__file-input"
        type="file"
        tabIndex={-1}
        accept=".json,application/json"
        onChange={onChange}
      />
      <div className="action-panel__file-picker">
        <button
          type="button"
          className="action-panel__file-button"
          aria-describedby="strategy-params-file-status"
          onClick={() => inputRef.current?.click()}
        >
          <span>{fileName ? "Replace params file" : "Choose params file"}</span>
          <span className="action-panel__file-type" aria-hidden="true">
            JSON
          </span>
        </button>
        {fileName && (
          <button
            type="button"
            className="action-panel__file-clear"
            aria-label={`Clear selected strategy params file ${fileName}`}
            onClick={onClear}
          >
            Clear
          </button>
        )}
      </div>
      <span
        id="strategy-params-file-status"
        className={`action-panel__file-status ${fileName ? "has-file" : ""}`}
        role="status"
        aria-live="polite"
      >
        {fileName || "Built-in strategy defaults"}
      </span>
    </div>
  );
}

function Field({ label, children, wide = false }) {
  const generatedId = useId();
  const isSingleControl =
    isValidElement(children) && (children.type === "input" || children.type === "select");
  const className = `action-panel__field ${wide ? "is-wide" : ""}`;

  if (isSingleControl) {
    const controlId = children.props.id || generatedId;
    return (
      <div className={className}>
        <label className="label" htmlFor={controlId}>
          {label}
        </label>
        {cloneElement(children, { id: controlId })}
      </div>
    );
  }

  return (
    <div
      className={`${className} action-panel__field-group`}
      role="group"
      aria-labelledby={generatedId}
    >
      <div id={generatedId} className="label">
        {label}
      </div>
      {children}
    </div>
  );
}
