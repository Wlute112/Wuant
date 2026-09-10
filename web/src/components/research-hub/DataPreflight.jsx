// THESIS: Review source evidence before committing research compute.
// OWN-WORLD: Existing Strip Recorder typography, neutral panels and hairlines.
// STORY: Inspect coverage, repair/import unusable bars, then launch research.
// FIRST VIEWPORT: Compact verdict followed by per-ticker evidence and recovery.
// FORM: Local extension of the existing research configuration form.
import { useEffect, useState } from "react";
import { api } from "../../lib/api.js";
import { isJobActive, jobStatusLabel } from "../../lib/jobs.js";
import DataReport from "./DataReport.jsx";
import ActiveResearchRun from "./ActiveResearchRun.jsx";

export function useDataPreflight(request, jobs = [], enabled = true) {
  const key = JSON.stringify(request);
  const repairs = JSON.stringify(jobs.filter((job) => job.kind === "data_repair").map((job) => [job.id, job.status]));
  const [revision, setRevision] = useState(0);
  const identity = JSON.stringify([key, repairs, revision, enabled]);
  const [state, setState] = useState(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const deadline = setTimeout(() => {
      setState({ identity, error: "Data preflight timed out" });
      controller.abort();
    }, 15000);
    const timer = setTimeout(() => {
      api.preflightData(JSON.parse(key), controller.signal)
        .then((report) => { if (!controller.signal.aborted) setState({ identity, report }); })
        .catch((error) => { if (!controller.signal.aborted) setState({ identity, error: error.message }); })
        .finally(() => clearTimeout(deadline));
    }, 250);
    return () => { clearTimeout(timer); clearTimeout(deadline); controller.abort(); };
  }, [key, identity, enabled]);
  const current = state?.identity === identity ? state : null;
  return { report: current?.report, error: current?.error,
    busy: enabled && !current, eligible: !!current?.report?.execution_eligible,
    refresh: () => setRevision((value) => value + 1) };
}


export default function DataPreflight({ state, request, jobs = [], onJobStarted, repairOptions }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [repairEdits, setRepairEdits] = useState({});
  const repairSettings = { ...repairOptions, ...repairEdits };
  const editRepair = (key, value) => setRepairEdits((current) => ({ ...current, [key]: value }));
  const repairJob = jobs.find((job) => job.kind === "data_repair" && job.config?.csv === request.csv);
  const active = isJobActive(repairJob);
  async function repair() {
    setPending(true); setError(null);
    try {
      const job = await api.repairData({ ...request, ibkr: repairSettings });
      onJobStarted(job);
    } catch (cause) { setError(cause.message); }
    finally { setPending(false); }
  }
  async function cancel() {
    setPending(true); setError(null);
    try { await api.cancelJob(repairJob.id); }
    catch (cause) { setError(cause.message); }
    finally { setPending(false); }
  }
  return <section className="data-preflight" aria-label="Research data preflight" aria-busy={state.busy}>
    <div className="data-preflight__heading">
      <strong role="status">{state.busy ? "Checking research data…" : state.error ? "Data status unavailable" : state.eligible ? "Data preflight passed" : "Execution research blocked"}</strong>
      <button type="button" disabled={state.busy || pending || active} onClick={state.refresh}>Recheck data</button>
    </div>
    {state.error && <p role="alert">{state.error}. Recheck before launching research.</p>}
    <DataReport report={state.report} />
    {repairOptions && <details>
      <summary>Repair with observed IBKR trade bars</summary>
      <p>Fetch replaces the selected CSV with the requested universe after validation. The original is retained as a backup. It requires TWS/Gateway and market-data permissions. To import instead, select a Data CSV above.</p>
      <div className="data-preflight__config">
        <label>IBKR host<input value={repairSettings.ibkr_host || "127.0.0.1"} onChange={(event) => editRepair("ibkr_host", event.target.value)} /></label>
        <label>IBKR port<input type="number" min="1" max="65535" value={repairSettings.ibkr_port || 7497} onChange={(event) => editRepair("ibkr_port", Number(event.target.value))} /></label>
        <label>Client ID<input type="number" min="0" value={repairSettings.ibkr_client_id ?? 71} onChange={(event) => editRepair("ibkr_client_id", Number(event.target.value))} /></label>
        <label>Years<input type="number" min="1" value={repairSettings.ibkr_years || 5} onChange={(event) => editRepair("ibkr_years", Number(event.target.value))} /></label>
        <label>Bar width<select value={repairSettings.ibkr_bar_hours || 4} onChange={(event) => editRepair("ibkr_bar_hours", Number(event.target.value))}>{[1, 2, 3, 4, 8, 24].map((hours) => <option key={hours} value={hours}>{hours === 24 ? "Daily" : `${hours} hours`}</option>)}</select></label>
        <label>Session<select value={repairSettings.include_extended_hours ? "extended" : "rth"} onChange={(event) => editRepair("include_extended_hours", event.target.value === "extended")}><option value="rth">Regular trading hours</option><option value="extended">Include extended hours</option></select></label>
      </div>
      <button type="button" disabled={pending || active} onClick={repair}>{pending ? "Submitting…" : "Fetch observed replacement"}</button>
      {active && <button type="button" disabled={pending || repairJob.status === "cancelling"} onClick={cancel}>Cancel data fetch</button>}
    </details>}
    {repairJob && <details><summary>Latest data repair: {jobStatusLabel(repairJob.status)} · progress, results & logs</summary><ActiveResearchRun job={repairJob} /></details>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
