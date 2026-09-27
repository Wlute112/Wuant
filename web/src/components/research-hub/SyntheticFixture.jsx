// THESIS: Generate a calendar-correct demonstration tape beside data preflight.
// OWN-WORLD: Strip Recorder's compact form, flat panel, mono evidence labels.
// STORY: Configure sessions, launch, inspect the schedule, then select its CSV.
// FIRST VIEWPORT: One disclosure under data evidence; result stays beside controls.
// FORM: Existing research data disclosure, Operate mode, incumbent design.
import { useEffect, useState } from "react";
import { api } from "../../lib/api.js";
import { isJobActive, jobStatusLabel } from "../../lib/jobs.js";
import { useInterval } from "../../hooks/useInterval.js";

function sameUniverse(left = [], right = []) {
  if (left.length !== right.length) return false;
  const sortedRight = [...right].sort();
  return [...left].sort().every((ticker, index) => ticker === sortedRight[index]);
}

export default function SyntheticFixture({ request, jobs, onJobStarted, onSelectCsv }) {
  const [days, setDays] = useState(1000);
  const [start, setStart] = useState("2021-06-28");
  const [seed, setSeed] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [progress, setProgress] = useState(null);
  const [progressError, setProgressError] = useState(null);
  const matchingJobs = jobs.filter((item) => item.kind === "synthetic_fixture"
    && item.config?.asset_class === request.asset_class
    && sameUniverse(item.config?.tickers, request.tickers));
  const job = matchingJobs.find((item) => item.id === selectedJobId) || matchingJobs[0];
  const active = isJobActive(job);

  async function refreshProgress() {
    if (!job?.id) return;
    try {
      setProgress(await api.getJobProgress(job.id));
      setProgressError(null);
    } catch (cause) {
      setProgressError(`Fixture progress unavailable: ${cause.message}`);
    }
  }
  useEffect(() => { setProgress(null); refreshProgress(); }, [job?.id, job?.status]);
  useInterval(refreshProgress, active ? 1000 : null);

  async function launch() {
    if (!Number.isInteger(Number(days)) || Number(days) < 1 || Number(days) > 10000 || !/^\d{4}-\d{2}-\d{2}$/.test(start)) {
      setError("Enter 1–10,000 sessions and a valid first date.");
      return;
    }
    if (seed !== "" && (!Number.isInteger(Number(seed)) || Number(seed) < 0 || Number(seed) >= 2**31)) {
      setError("Seed must be an integer from 0 to 2,147,483,647.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const created = await api.generateSyntheticFixture({
        asset_class: request.asset_class,
        tickers: request.tickers || [],
        days: Number(days), start,
        seed: seed === "" ? null : Number(seed),
      });
      setSelectedJobId(created.id);
      setProgress(null);
      onJobStarted(created);
    } catch (cause) { setError(cause.message); }
    finally { setPending(false); }
  }

  async function cancel() {
    setPending(true); setError(null);
    try { await api.cancelJob(job.id); }
    catch (cause) { setError(cause.message); }
    finally { setPending(false); }
  }

  return <details>
    <summary>Generate synthetic {request.asset_class === "equity" ? "NYSE session" : "24/7 crypto"} fixture</summary>
    <p>Demonstration prices and volume. Equity bars use the versioned XNYS calendar, including holidays, early closes and UTC daylight-saving changes. These bars are not market evidence.</p>
    <div>
      <div className="data-preflight__config">
        <label>Sessions<input type="number" min="1" max="10000" value={days} onChange={(event) => setDays(event.target.value)} /></label>
        <label>First eligible date<input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></label>
        <label>Seed (optional)<input type="number" min="0" max="2147483647" value={seed} onChange={(event) => setSeed(event.target.value)} placeholder="Fresh random seed" /></label>
      </div>
      <p>Tickers: {(request.tickers || []).join(" · ") || "Choose tickers above"}.</p>
      <button type="button" onClick={launch} disabled={pending || active || !request.tickers?.length}>{pending ? "Submitting…" : "Generate fixture"}</button>
      {active && <button type="button" disabled={pending || job.status === "cancelling"} onClick={cancel}>Cancel generation</button>}
    </div>
    {job && <div aria-live="polite" className="data-preflight__fixture-result">
      <strong>Latest fixture: {jobStatusLabel(job.status)}</strong>
      <p>Origin: {job.config.asset_class} · {job.config.tickers.join(" · ")}</p>
      {active && <p role="status">{progress?.phase_label || "Generating synthetic bars…"}</p>}
      {job.status === "completed" && progress?.csv && <>
        <p>{progress.sessions} sessions · {progress.rows} bars · seed {progress.seed}<br />
          {progress.start} → {progress.end}<br />
          {progress.calendar}{progress.calendar_version ? ` calendar v${progress.calendar_version}` : ""} · {progress.early_close_sessions} early closes</p>
        {progress.sessions <= 150 && <p>Short fixture: the default strategy warms up for 150 bars and needs additional bars to trade or evaluate.</p>}
        <button type="button" onClick={() => onSelectCsv(progress.csv, `${progress.calendar} synthetic fixture · ${job.id}.csv`)}>Use fixture for research</button>
      </>}
      {job.failure_reason && <p role="alert">{job.failure_reason}</p>}
      {progressError && <p role="alert">{progressError}</p>}
    </div>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
