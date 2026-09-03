import { cloneElement, isValidElement, useEffect, useId, useRef, useState } from "react";

import { api } from "../../lib/api.js";
import { regimeWindowForBarHours } from "../../lib/assetProfiles.js";
import { formatTime } from "../../lib/format.js";
import { isJobActive } from "../../lib/jobs.js";
import FeaturePanel, { DEFAULT_FEATURES, DEFAULT_SEARCH_MODES } from "../features/FeaturePanel.jsx";
import RiskPanel, { DEFAULT_RISK, toRiskOverrides } from "../features/RiskPanel.jsx";
import "./action-panel.css";

const LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS DEPLOYS REAL CAPITAL";
const PAPER_PORTS = new Set([7497, 4002]);
const SETTINGS_STORAGE_KEY = "quant-dashboard.action-settings.v1";
const UNKNOWN_LIVE_READINESS = {
  live_capital_enabled: false,
  code: "READINESS_STATUS_UNKNOWN",
  gates: [],
  incomplete: [],
};

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
  workflow = "backtest",
  assetClass: selectedAssetClass,
  assetProfiles = {},
  onAssetClassChange,
  runs = [],
  jobs = [],
  brokerStatus = {},
}) {
  const [persisted] = useState(readPersistedSettings);
  const tab = workflow;
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

  const assetClass = selectedAssetClass || persisted.assetClass || "crypto";
  const profile = assetProfiles[assetClass] || {};
  const [tickers, setTickers] = useState(
    persisted.tickers ?? (profile.defaults?.tickers || ["BTC", "ETH", "SOL", "XRP", "DOGE"]).join(","),
  );
  const [csvPath, setCsvPath] = useState(persisted.csvPath ?? "");
  const [cash, setCash] = useState(persisted.cash ?? 5000);
  const [trials, setTrials] = useState(persisted.trials ?? 40);
  const [stopMode, setStopMode] = useState(persisted.stopMode || "trials"); // "trials" | "score"
  const [targetScore, setTargetScore] = useState(persisted.targetScore ?? 1.5);
  const [finalTestFrac, setFinalTestFrac] = useState(persisted.finalTestFrac ?? 0.2);
  const [walkForwardFolds, setWalkForwardFolds] = useState(persisted.walkForwardFolds ?? 5);
  const [embargoBars, setEmbargoBars] = useState(persisted.embargoBars ?? 0);
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
  const [allowShorts, setAllowShorts] = useState(false);
  const [shortControlClientId, setShortControlClientId] = useState(persisted.shortControlClientId ?? 29);
  const [shortBorrowApiUrl, setShortBorrowApiUrl] = useState(
    persisted.shortBorrowApiUrl ?? "ftp://shortstock@ftp2.interactivebrokers.com/usa.txt",
  );
  const [shortBorrowApiVerifyTls, setShortBorrowApiVerifyTls] = useState(
    Boolean(persisted.shortBorrowApiVerifyTls),
  );
  const [shortMaxBorrowFeePct, setShortMaxBorrowFeePct] = useState(persisted.shortMaxBorrowFeePct ?? 5);
  const [shortMinMarginCushionPct, setShortMinMarginCushionPct] = useState(
    persisted.shortMinMarginCushionPct ?? 20,
  );
  const [shortLocateBufferRatio, setShortLocateBufferRatio] = useState(
    persisted.shortLocateBufferRatio ?? 1.25,
  );
  const [shortRecallGraceSecs, setShortRecallGraceSecs] = useState(
    persisted.shortRecallGraceSecs ?? 60,
  );
  const [barHours, setBarHours] = useState(persisted.barHours ?? profile.defaults?.bar_hours ?? 4);
  const [includeExtendedHours, setIncludeExtendedHours] = useState(Boolean(persisted.includeExtendedHours));
  const [liveConfirmation, setLiveConfirmation] = useState("");
  const [liveReadiness, setLiveReadiness] = useState({
    ...UNKNOWN_LIVE_READINESS,
    loading: true,
  });

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
  const optimizeRuns = runs.filter((r) => r.kind === "optimize" && (r.asset_class || "crypto") === assetClass);
  const paperJob = jobs.find(
    (job) => job.kind === "paper"
      && !job.parent_job_id
      && isJobActive(job)
      && (job.config?.asset_class || "crypto") === assetClass,
  );
  const previousAssetClass = useRef(assetClass);
  const previousTab = useRef(tab);
  const preserveProfileFields = useRef(false);

  function applyProfileDefaults(nextAssetClass) {
    const next = assetProfiles[nextAssetClass];
    if (!next) return;
    setTickers(next.defaults.tickers.join(","));
    setCsvPath("");
    setTargetScore(next.defaults.target_score);
    setBarHours(next.defaults.bar_hours);
    setIbkrBarHours(next.defaults.bar_hours);
    setPrimaryExchange(next.defaults.primary_exchange || "");
    setAllowShorts(false);
    setIncludeExtendedHours(false);
    setSourceRunId("");
    setLoadedParams(null);
    setResumeRunId("");
    setTradingParams(null);
    setTradingParamsName("");
    if (tradingParamsInput.current) tradingParamsInput.current.value = "";
    setFeatures((current) => ({
      ...current,
      regime_window: next.defaults.regime_window,
      regime_bull_threshold: next.defaults.regime_bull_threshold,
      regime_bear_threshold: next.defaults.regime_bear_threshold,
    }));
  }

  function changeAssetClass(nextAssetClass) {
    if (nextAssetClass === assetClass) return;
    onAssetClassChange?.(nextAssetClass);
  }

  useEffect(() => {
    if (previousAssetClass.current === assetClass) return;
    if (preserveProfileFields.current) {
      preserveProfileFields.current = false;
    } else {
      applyProfileDefaults(assetClass);
    }
    previousAssetClass.current = assetClass;
  }, [assetClass, assetProfiles]);

  useEffect(() => {
    if (previousTab.current !== tab) {
      setAllowShorts(false);
      previousTab.current = tab;
    }
    setError(null);
  }, [tab]);

  useEffect(() => {
    if (tab !== "live") return undefined;
    let active = true;

    async function refreshReadiness() {
      try {
        const status = await api.getLiveReadiness();
        if (active) setLiveReadiness({ ...status, loading: false });
      } catch (readinessError) {
        if (active) {
          setLiveReadiness({
            ...UNKNOWN_LIVE_READINESS,
            loading: false,
            error: readinessError.message,
          });
        }
      }
    }

    refreshReadiness();
    const timer = window.setInterval(refreshReadiness, 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [tab]);

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
          finalTestFrac,
          walkForwardFolds,
          embargoBars,
          host,
          port,
          livePort,
          clientId,
          accountId,
          tradingParams,
          tradingParamsName,
          primaryExchange,
          shortControlClientId,
          shortBorrowApiUrl,
          shortBorrowApiVerifyTls,
          shortMaxBorrowFeePct,
          shortMinMarginCushionPct,
          shortLocateBufferRatio,
          shortRecallGraceSecs,
          barHours,
          includeExtendedHours,
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
    finalTestFrac,
    walkForwardFolds,
    embargoBars,
    host,
    port,
    livePort,
    clientId,
    accountId,
    tradingParams,
    tradingParamsName,
    primaryExchange,
    shortControlClientId,
    shortBorrowApiUrl,
    shortBorrowApiVerifyTls,
    shortMaxBorrowFeePct,
    shortMinMarginCushionPct,
    shortLocateBufferRatio,
    shortRecallGraceSecs,
    barHours,
    includeExtendedHours,
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

  async function handleTradingParamsFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError(null);
    try {
      const parsed = JSON.parse(await file.text());
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("the file must contain a JSON object");
      }
      if (parsed.asset_class && parsed.asset_class !== assetClass) {
        throw new Error(
          `profile mismatch: this file was optimized for ${parsed.asset_class}, but ${assetClass} is selected`,
        );
      }
      if (parsed.bar_interval_minutes != null) {
        const trainedMinutes = Number(parsed.bar_interval_minutes);
        const trainedHours = trainedMinutes / 60;
        if (![1, 2, 3, 4, 8, 24].includes(trainedHours)) {
          throw new Error(
            `the file was trained on ${trainedMinutes}-minute bars, which IBKR cannot stream continuously; retrain on 1/2/3/4/8-hour or daily bars`,
          );
        }
        setBarHours(trainedHours);
      } else if (parsed.ibkr_bar_hours) {
        setBarHours(Number(parsed.ibkr_bar_hours));
      }
      if (parsed.include_extended_hours != null) {
        setIncludeExtendedHours(Boolean(parsed.include_extended_hours));
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
        include_extended_hours: assetClass === "equity" && includeExtendedHours,
      },
    };
  }

  function shortControlPayload() {
    return {
      client_id: Number(shortControlClientId),
      borrow_api_url: shortBorrowApiUrl.trim(),
      borrow_api_verify_tls: shortBorrowApiVerifyTls,
      max_borrow_fee_pct: Number(shortMaxBorrowFeePct),
      min_margin_cushion_pct: Number(shortMinMarginCushionPct),
      locate_buffer_ratio: Number(shortLocateBufferRatio),
      recall_grace_secs: Number(shortRecallGraceSecs),
    };
  }

  async function submit() {
    setPending(true);
    setError(null);
    try {
      if (tab === "live" && !liveReadiness.live_capital_enabled) {
        throw new Error(
          liveReadiness.error
            ? `Live readiness is unknown: ${liveReadiness.error}`
            : `Live capital is disabled (${liveReadiness.code}).`,
        );
      }
      if (tab === "paper" && assetClass === "crypto") {
        throw new Error(
          "IBKR paper accounts do not support spot-crypto execution. Use the crypto backtest/demo feed or switch to Equity paper trading.",
        );
      }
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
          final_test_frac: Number(finalTestFrac),
          walk_forward_folds: Number(walkForwardFolds),
          embargo_bars: Number(embargoBars),
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
          allow_shorts: assetClass === "equity" && allowShorts,
          ...(assetClass === "equity" && allowShorts
            ? { short_controls: shortControlPayload() }
            : {}),
          bar_hours: Number(barHours),
          include_extended_hours: assetClass === "equity" && includeExtendedHours,
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
          allow_shorts: assetClass === "equity" && allowShorts,
          ...(assetClass === "equity" && allowShorts
            ? { short_controls: shortControlPayload() }
            : {}),
          bar_hours: Number(barHours),
          include_extended_hours: assetClass === "equity" && includeExtendedHours,
          host,
          port: Number(livePort),
          client_id: Number(clientId),
          account_id: accountId || null,
          cash: Number(cash),
          params: { ...(tradingParams || {}), ...toRiskOverrides(risk) },
          confirm: liveConfirmation,
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
        {tab === "backtest"
          ? "Backtest controls"
          : tab === "optimize"
            ? "Optuna controls"
            : tab === "paper"
              ? "Paper trading controls"
              : "Live trading controls"}
      </h2>
      <div
        id={`workflow-panel-${tab}`}
        className="action-panel__form"
      >
        {(tab === "backtest" || tab === "optimize") && (
          <>
            <div className="action-panel__fields action-panel__fields--research">
              <Field label="Tickers">
                <input value={tickers} onChange={(e) => setTickers(e.target.value)} />
              </Field>
              <Field label="Asset class">
                <select value={assetClass} onChange={(e) => changeAssetClass(e.target.value)}>
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
                  <Field label="Resume interrupted study (optional)">
                    <input
                      value={resumeRunId}
                      onChange={(e) => setResumeRunId(e.target.value)}
                      placeholder="study run id"
                    />
                  </Field>
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
                      <Field label={`Optimization score target (${profile.scoring?.short_label || "ratio"} − activity)`}>
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
                  <Field label="Untouched final test">
                    <select value={finalTestFrac} onChange={(e) => setFinalTestFrac(e.target.value)}>
                      <option value={0.15}>Newest 15%</option>
                      <option value={0.2}>Newest 20%</option>
                    </select>
                  </Field>
                  <Field label="Walk-forward folds">
                    <select value={walkForwardFolds} onChange={(e) => setWalkForwardFolds(e.target.value)}>
                      {[5, 6, 7, 8].map((count) => (
                        <option key={count} value={count}>{count} folds</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Extra embargo bars">
                    <input
                      type="number"
                      min={0}
                      value={embargoBars}
                      onChange={(e) => setEmbargoBars(e.target.value)}
                      title="The effective embargo is never smaller than the trial's forecast horizon."
                    />
                  </Field>
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
                Resuming interrupted study {resumeRunId}. Data, universe, profile,
                features, risk, and validation settings must match exactly. Finalized
                studies cannot be resumed after their outer test has been consumed.
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
                      {assetClass === "equity" && (
                        <Field label="Include extended hours">
                          <input
                            type="checkbox"
                            checked={includeExtendedHours}
                            onChange={(e) => {
                              const checked = e.target.checked;
                              setIncludeExtendedHours(checked);
                              setFeatures((current) => ({
                                ...current,
                                regime_window: regimeWindowForBarHours(assetClass, ibkrBarHours, checked),
                              }));
                            }}
                          />
                        </Field>
                      )}
                      {dataFetchMode === "replace" && (
                        <Field label="Replacement bar frequency">
                          <select
                            value={ibkrBarHours}
                            onChange={(e) => {
                              const hours = Number(e.target.value);
                              setIbkrBarHours(hours);
                              setFeatures((current) => ({
                                ...current,
                                regime_window: regimeWindowForBarHours(assetClass, hours),
                              }));
                            }}
                          >
                            <option value={1}>1 hour</option>
                            <option value={2}>2 hours</option>
                            <option value={3}>3 hours</option>
                            <option value={4}>4 hours</option>
                            <option value={8}>8 hours</option>
                            <option value={12}>12 hours</option>
                            <option value={24}>24 hours (profile default)</option>
                          </select>
                        </Field>
                      )}
                    </div>
                  )}
                  {dataFetchMode === "replace" && (
                    <div className="action-panel__loaded-params">
                      Replacement is destructive to the selected CSV: existing tickers and
                      their old-frequency bars are removed. The 20-session regime lookback is
                      rescaled to {features.regime_window} completed bars; the daily-loss rail
                      still resets on elapsed UTC days.
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
            <div className="action-panel__session-note">
              <span className="label">{profile.short_label} execution contract</span>
              <span>{profile.market?.session} · {profile.market?.venue} · {profile.market?.quantity} · {profile.market?.fee_model}</span>
            </div>
            {assetClass === "crypto" && !paperJob && (
              <div className="action-panel__availability-note" role="status">
                IBKR paper accounts do not support spot-crypto execution. Crypto remains available for backtests and the demonstration tape; select Equity to start a broker paper session.
              </div>
            )}
            {!paperJob && <div className="action-panel__fields action-panel__fields--trading">
              <Field label="Asset class">
                <select value={assetClass} onChange={(e) => changeAssetClass(e.target.value)}>
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
                  <Field label="Short positions">
                    <input
                      type="checkbox"
                      checked={allowShorts}
                      onChange={(event) => setAllowShorts(event.target.checked)}
                    />
                  </Field>
                  <Field label="Include extended hours">
                    <input
                      type="checkbox"
                      checked={includeExtendedHours}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        setIncludeExtendedHours(checked);
                        setFeatures((current) => ({
                          ...current,
                          regime_window: regimeWindowForBarHours(assetClass, barHours, checked),
                        }));
                      }}
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
                <Field label="Completed bar cadence">
                  <BarHoursSelect
                    value={barHours}
                    onChange={(hours) => {
                      setBarHours(hours);
                      setFeatures((current) => ({
                        ...current,
                        regime_window: regimeWindowForBarHours(assetClass, hours, includeExtendedHours),
                      }));
                    }}
                  />
                </Field>
                {assetClass === "equity" && allowShorts && (
                  <>
                    <Field label="Short-control client ID">
                      <input type="number" min="0" value={shortControlClientId} onChange={(event) => setShortControlClientId(event.target.value)} />
                    </Field>
                    <Field label="IBKR borrow feed URL" wide>
                      <input value={shortBorrowApiUrl} onChange={(event) => setShortBorrowApiUrl(event.target.value)} />
                    </Field>
                    <Field label="Verify HTTP feed TLS">
                      <input type="checkbox" checked={shortBorrowApiVerifyTls} onChange={(event) => setShortBorrowApiVerifyTls(event.target.checked)} />
                    </Field>
                    <Field label="Maximum borrow fee (%)">
                      <input type="number" min="0.01" max="100" step="0.05" value={shortMaxBorrowFeePct} onChange={(event) => setShortMaxBorrowFeePct(event.target.value)} />
                    </Field>
                    <Field label="Minimum margin cushion (%)">
                      <input type="number" min="0.01" max="99" step="1" value={shortMinMarginCushionPct} onChange={(event) => setShortMinMarginCushionPct(event.target.value)} />
                    </Field>
                    <Field label="Locate buffer">
                      <input type="number" min="1" max="10" step="0.05" value={shortLocateBufferRatio} onChange={(event) => setShortLocateBufferRatio(event.target.value)} />
                    </Field>
                    <Field label="Recall grace (seconds)">
                      <input type="number" min="1" max="3600" step="1" value={shortRecallGraceSecs} onChange={(event) => setShortRecallGraceSecs(event.target.value)} />
                    </Field>
                  </>
                )}
              </div>
            </details>}
            <div className="action-panel__actions">
              <button
                className={paperJob ? "button-danger" : "button-primary"}
                disabled={pending || paperJob?.status === "cancelling" || (!paperJob && assetClass === "crypto")}
                onClick={paperJob ? stopPaperTrading : submit}
              >
                {pending
                  ? (paperJob ? "Stopping…" : "Starting…")
                  : paperJob?.status === "cancelling"
                    ? "Stopping Paper Trading…"
                    : (paperJob ? "Stop Paper Trading" : "Start Paper Trading")}
              </button>
            </div>
          </>
        )}

        {tab === "live" && (
          <>
            <RiskPanel value={risk} onChange={setRisk} />
            <div className="action-panel__readiness-lock" role="alert">
              <span className="label">
                {liveReadiness.loading
                  ? "LIVE CAPITAL · CHECKING READINESS"
                  : liveReadiness.live_capital_enabled
                    ? "LIVE CAPITAL · READINESS APPROVED"
                    : "LIVE CAPITAL · P0 LOCKED"}
              </span>
              <span>
                {liveReadiness.error
                  ? `Readiness status unavailable: ${liveReadiness.error}. Controls remain locked.`
                  : liveReadiness.live_capital_enabled
                    ? "All reviewed production-readiness gates passed."
                    : `${liveReadiness.incomplete.length || "All"} production-readiness gates remain unapproved. ${liveReadiness.code}.`}
              </span>
            </div>
            <div className="action-panel__session-note">
              <span className="label">{profile.short_label} execution contract</span>
              <span>{profile.market?.session} · {profile.market?.venue} · {profile.market?.quantity} · {profile.market?.fee_model}</span>
            </div>
            <div className="action-panel__fields action-panel__fields--trading">
              <Field label="Asset class">
                <select value={assetClass} onChange={(e) => changeAssetClass(e.target.value)}>
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
                  <Field label="Short positions">
                    <input
                      type="checkbox"
                      checked={allowShorts}
                      onChange={(event) => setAllowShorts(event.target.checked)}
                    />
                  </Field>
                  <Field label="Include extended hours">
                    <input
                      type="checkbox"
                      checked={includeExtendedHours}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        setIncludeExtendedHours(checked);
                        setFeatures((current) => ({
                          ...current,
                          regime_window: regimeWindowForBarHours(assetClass, barHours, checked),
                        }));
                      }}
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
              <Field label="Type the exact phrase to arm live trading" wide>
                <input
                  className="action-panel__confirm-input"
                  value={liveConfirmation}
                  onChange={(event) => setLiveConfirmation(event.target.value)}
                  placeholder={LIVE_CONFIRM_PHRASE}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={!liveReadiness.live_capital_enabled}
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
                    disabled={!liveReadiness.live_capital_enabled}
                  />
                </Field>
                <Field label="Client ID">
                  <input type="number" value={clientId} onChange={(e) => setClientId(e.target.value)} />
                </Field>
                <Field label="Live account ID">
                  <input value={accountId} onChange={(e) => setAccountId(e.target.value)} />
                </Field>
                <Field label="Completed bar cadence">
                  <BarHoursSelect
                    value={barHours}
                    onChange={(hours) => {
                      setBarHours(hours);
                      setFeatures((current) => ({
                        ...current,
                        regime_window: regimeWindowForBarHours(assetClass, hours, includeExtendedHours),
                      }));
                    }}
                  />
                </Field>
                {assetClass === "equity" && allowShorts && (
                  <>
                    <Field label="Short-control client ID">
                      <input type="number" min="0" value={shortControlClientId} onChange={(event) => setShortControlClientId(event.target.value)} />
                    </Field>
                    <Field label="IBKR borrow feed URL" wide>
                      <input value={shortBorrowApiUrl} onChange={(event) => setShortBorrowApiUrl(event.target.value)} />
                    </Field>
                    <Field label="Verify HTTP feed TLS">
                      <input type="checkbox" checked={shortBorrowApiVerifyTls} onChange={(event) => setShortBorrowApiVerifyTls(event.target.checked)} />
                    </Field>
                    <Field label="Maximum borrow fee (%)">
                      <input type="number" min="0.01" max="100" step="0.05" value={shortMaxBorrowFeePct} onChange={(event) => setShortMaxBorrowFeePct(event.target.value)} />
                    </Field>
                    <Field label="Minimum margin cushion (%)">
                      <input type="number" min="0.01" max="99" step="1" value={shortMinMarginCushionPct} onChange={(event) => setShortMinMarginCushionPct(event.target.value)} />
                    </Field>
                    <Field label="Locate buffer">
                      <input type="number" min="1" max="10" step="0.05" value={shortLocateBufferRatio} onChange={(event) => setShortLocateBufferRatio(event.target.value)} />
                    </Field>
                    <Field label="Recall grace (seconds)">
                      <input type="number" min="1" max="3600" step="1" value={shortRecallGraceSecs} onChange={(event) => setShortRecallGraceSecs(event.target.value)} />
                    </Field>
                  </>
                )}
              </div>
            </details>
            <div className="action-panel__actions">
              <button
                className="button-danger"
                disabled={
                  pending ||
                  !liveReadiness.live_capital_enabled ||
                  liveConfirmation !== LIVE_CONFIRM_PHRASE ||
                  !Number(livePort) ||
                  PAPER_PORTS.has(Number(livePort))
                }
                onClick={submit}
              >
                {pending
                  ? "Arming…"
                  : liveReadiness.live_capital_enabled
                    ? "Start Live Trading"
                    : "Live Trading Locked"}
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

function BarHoursSelect({ value, onChange }) {
  return (
    <select value={value} onChange={(event) => onChange(Number(event.target.value))}>
      <option value={1}>1 hour</option>
      <option value={2}>2 hours</option>
      <option value={3}>3 hours</option>
      <option value={4}>4 hours</option>
      <option value={8}>8 hours</option>
      <option value={24}>1 day (profile default)</option>
    </select>
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
