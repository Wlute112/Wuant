import { useCallback, useEffect, useMemo, useState } from "react";

import { useInterval } from "../../hooks/useInterval.js";
import { api } from "../../lib/api.js";
import { isJobActive } from "../../lib/jobs.js";

const PROMOTION_PHRASE = "CONSUME OUTER HOLDOUT";

function parseNumbers(value) {
  return value.split(/[\s,]+/).map(Number).filter(Number.isFinite);
}

function optionalNumber(value) {
  return value === "" ? null : Number(value);
}

export default function CampaignPanel({ assetClass, profile, jobs, onJobStarted }) {
  const [campaigns, setCampaigns] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState(null);
  const [campaignId, setCampaignId] = useState(`${assetClass}_daily_v1`);
  const [seeds, setSeeds] = useState("42 43 44 45");
  const [trials, setTrials] = useState(100);
  const [tickers, setTickers] = useState((profile?.defaults?.tickers || []).join(","));
  const [csv, setCsv] = useState(assetClass === "equity" ? "quant/data/equity_bars.csv" : "quant/data/ibkr_bars.csv");
  const [cash, setCash] = useState(5000);
  const [finalTestFrac, setFinalTestFrac] = useState(0.2);
  const [walkForwardFolds, setWalkForwardFolds] = useState(5);
  const [embargoBars, setEmbargoBars] = useState(0);
  const [workers, setWorkers] = useState("");
  const [memoryBudgetGb, setMemoryBudgetGb] = useState("");
  const [workerMemoryGb, setWorkerMemoryGb] = useState("");
  const [finalists, setFinalists] = useState(5);
  const [topN, setTopN] = useState(10);
  const [maxClusterDistance, setMaxClusterDistance] = useState(0.2);
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(null);
  const [error, setError] = useState(null);
  const hasActiveCampaignJob = jobs.some((job) => job.kind?.startsWith("campaign_") && isJobActive(job));

  const refresh = useCallback(async () => {
    try {
      const list = await api.listCampaigns();
      setCampaigns(list);
      setSelectedId((current) => current || list[0]?.campaign_id || "");
      setError(null);
    } catch (loadError) {
      setError(`Campaign registry unavailable: ${loadError.message}`);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useInterval(refresh, hasActiveCampaignJob ? 4000 : null);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let disposed = false;
    api.getCampaign(selectedId)
      .then((value) => { if (!disposed) setDetail(value); })
      .catch((loadError) => { if (!disposed) setError(loadError.message); });
    return () => { disposed = true; };
  }, [campaigns, selectedId]);

  const stage = useMemo(() => {
    if (!detail) return 0;
    if (["PROMOTED", "REJECTED_OUTER_HOLDOUT", "CONSUMED_FAILED"].includes(detail.promotion_status)) return 4;
    if (detail.robustness_ready) return 3;
    if (detail.comparison_ready) return 2;
    if (detail.studies_complete >= 3) return 1;
    return 0;
  }, [detail]);

  async function launch(kind, body) {
    setPending(kind);
    setError(null);
    try {
      const methods = {
        seeds: api.startCampaignSeeds,
        compare: api.startCampaignCompare,
        robustness: api.startCampaignRobustness,
        promote: api.startCampaignPromote,
      };
      const job = await methods[kind](body);
      onJobStarted(job);
      if (kind === "seeds") setSelectedId(campaignId);
      setConfirmation("");
      await refresh();
    } catch (launchError) {
      setError(launchError.message);
    } finally {
      setPending(null);
    }
  }

  const stageRequest = {
    campaign_id: selectedId,
    finalists: Number(finalists),
    top_n: Number(topN),
    max_cluster_distance: Number(maxClusterDistance),
    workers: optionalNumber(workers),
    memory_budget_gb: optionalNumber(memoryBudgetGb),
    worker_memory_gb: optionalNumber(workerMemoryGb),
  };

  return (
    <div className="campaign-panel">
      <div className="campaign-panel__builder">
        <label>Campaign ID<input value={campaignId} onChange={(event) => setCampaignId(event.target.value)} /></label>
        <label>Independent seeds<input value={seeds} onChange={(event) => setSeeds(event.target.value)} /></label>
        <label>Trials per seed<input type="number" min="100" max="150" value={trials} onChange={(event) => setTrials(event.target.value)} /></label>
        <label>Universe<input value={tickers} onChange={(event) => setTickers(event.target.value)} /></label>
        <label className="is-wide">Source CSV<input value={csv} onChange={(event) => setCsv(event.target.value)} /></label>
        <details className="campaign-panel__advanced is-wide">
          <summary>Validation and compute settings</summary>
          <div className="campaign-panel__advanced-grid">
            <label>Cash<input type="number" min="1" value={cash} onChange={(event) => setCash(event.target.value)} /></label>
            <label>Outer holdout fraction<input type="number" min="0.01" max="0.49" step="0.01" value={finalTestFrac} onChange={(event) => setFinalTestFrac(event.target.value)} /></label>
            <label>Walk-forward folds<input type="number" min="2" max="10" value={walkForwardFolds} onChange={(event) => setWalkForwardFolds(event.target.value)} /></label>
            <label>Embargo bars<input type="number" min="0" value={embargoBars} onChange={(event) => setEmbargoBars(event.target.value)} /></label>
            <label>Workers<input type="number" min="0" placeholder="Auto" value={workers} onChange={(event) => setWorkers(event.target.value)} /></label>
            <label>Total memory GB<input type="number" min="0" step="0.5" placeholder="Auto" value={memoryBudgetGb} onChange={(event) => setMemoryBudgetGb(event.target.value)} /></label>
            <label>Worker memory GB<input type="number" min="0.1" step="0.1" placeholder="Auto" value={workerMemoryGb} onChange={(event) => setWorkerMemoryGb(event.target.value)} /></label>
            <label>Finalists<input type="number" min="5" max="10" value={finalists} onChange={(event) => setFinalists(event.target.value)} /></label>
            <label>Top trials per seed<input type="number" min="5" max="10" value={topN} onChange={(event) => setTopN(event.target.value)} /></label>
            <label>Cluster distance<input type="number" min="0.01" max="1" step="0.01" value={maxClusterDistance} onChange={(event) => setMaxClusterDistance(event.target.value)} /></label>
          </div>
        </details>
        <button
          type="button"
          disabled={pending || parseNumbers(seeds).length < 3}
          onClick={() => launch("seeds", {
            campaign_id: campaignId,
            seeds: parseNumbers(seeds),
            trials: Number(trials),
            csv,
            asset_class: assetClass,
            tickers: tickers.split(",").map((value) => value.trim().toUpperCase()).filter(Boolean),
            cash: Number(cash),
            final_test_frac: Number(finalTestFrac),
            walk_forward_folds: Number(walkForwardFolds),
            embargo_bars: Number(embargoBars),
            workers: optionalNumber(workers),
            memory_budget_gb: optionalNumber(memoryBudgetGb),
            worker_memory_gb: optionalNumber(workerMemoryGb),
          })}
        >
          {pending === "seeds" ? "Starting…" : "Start multi-seed campaign"}
        </button>
      </div>

      <div className="campaign-panel__review">
        <label>Saved campaign
          <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            <option value="">Select campaign</option>
            {campaigns.map((item) => <option key={item.campaign_id}>{item.campaign_id}</option>)}
          </select>
        </label>
        <ol className="campaign-ladder">
          <li className={stage >= 1 ? "is-complete" : "is-current"}><span>Seed studies</span><strong>{detail ? `${detail.studies_complete}/${detail.studies_total}` : "—"}</strong></li>
          <li className={stage >= 2 ? "is-complete" : stage === 1 ? "is-current" : ""}><span>Consensus</span><strong>{detail?.comparison_ready ? "READY" : "WAITING"}</strong></li>
          <li className={stage >= 3 ? "is-complete" : stage === 2 ? "is-current" : ""}><span>Robustness</span><strong>{detail?.robustness_ready ? "READY" : "WAITING"}</strong></li>
          <li className={stage >= 4 ? "is-complete" : stage === 3 ? "is-current" : ""}><span>Outer holdout</span><strong>{detail?.promotion_status || "UNTOUCHED"}</strong></li>
        </ol>
        <div className="campaign-panel__actions">
          <button type="button" disabled={!selectedId || stage < 1 || pending} onClick={() => launch("compare", stageRequest)}>Build consensus</button>
          <button type="button" disabled={!selectedId || stage < 2 || pending} onClick={() => launch("robustness", stageRequest)}>Run robustness suite</button>
        </div>
        <div className="campaign-panel__holdout">
          <p>The final action consumes the outer holdout exactly once. A failed or interrupted evaluation cannot be repeated.</p>
          <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={PROMOTION_PHRASE} aria-label="Outer holdout confirmation phrase" />
          <button type="button" disabled={stage < 3 || confirmation !== PROMOTION_PHRASE || pending} onClick={() => launch("promote", { ...stageRequest, confirm: confirmation })}>Evaluate outer holdout</button>
        </div>
        {error && <p className="campaign-panel__error" role="alert">{error}</p>}
      </div>
    </div>
  );
}
