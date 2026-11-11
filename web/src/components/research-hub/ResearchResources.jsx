// Extend the instrument deck with one shared capacity ledger. Researchers
// inspect reservations before launch, cancel queued work, and review sampled
// memory without leaving the evidence workbench. Existing tokens own the style.
import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api.js";
import { useInterval } from "../../hooks/useInterval.js";
import "./research-resources.css";

const gib = (value) => value == null ? "Unmeasured" : `${Number(value).toFixed(2)} GiB`;

export function ResourceReading({ resources }) {
  if (!resources) return null;
  return <p className="research-resources__reading" role="status">
    {resources.state === "queued" ? `Queued${resources.queue_position ? ` · position ${resources.queue_position}` : ""} · waiting for shared host capacity` : "Host reservation"}
    {` · ${resources.cpu_slots} CPU slots · ${gib(resources.memory_gb)}`}
    {` · sampled peak RSS: ${gib(resources.peak_rss_gb)}`}
  </p>;
}

export default function ResearchResources({ jobs = [], onJobUpdated }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [pollError, setPollError] = useState(null);
  const [token, setToken] = useState("");
  const [cpu, setCpu] = useState("");
  const [memory, setMemory] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const load = useCallback(async () => {
    try { setReport(await api.getResearchResources()); setPollError(null); }
    catch (cause) { setPollError(`Host capacity unavailable: ${cause.message}. Displayed measurements are stale.`); }
  }, []);
  useEffect(() => { load(); }, [load]);
  useInterval(load, 3000);
  async function save(event) {
    event.preventDefault();
    setBusy(true); setSaved(false); setError(null);
    try {
      setReport(await api.saveResearchResources(token, { cpu_slots: Number(cpu), memory_gb: Number(memory) }));
      setError(null); setSaved(true);
    } catch (cause) { setError(`Limits were not saved: ${cause.message}`); }
    finally { setBusy(false); }
  }
  async function cancel(id) {
    setBusy(true); setError(null);
    try { onJobUpdated?.(await api.cancelJob(id)); await load(); }
    catch (cause) { setError(`Cancellation failed: ${cause.message}`); }
    finally { setBusy(false); }
  }
  return <details className="research-resources">
    <summary>Host research capacity{report && !pollError ? ` · ${report.requests.filter(row => row.state === "queued").length} queued` : " · unknown"}</summary>
    {error && <p role="alert">{error}</p>}
    {pollError && <p role="alert">{pollError}</p>}
    {(!report || pollError) && <button type="button" onClick={load}>Retry host capacity</button>}
    {report && <>
      <p role="status">{pollError ? "Last known: " : ""}{report.used.cpu_slots} / {report.limits.cpu_slots} CPU slots reserved · {gib(report.used.memory_gb)} / {gib(report.limits.memory_gb)} reserved</p>
      <p>Service reserve: {report.host.compute_cores - report.ceiling.cpu_slots} compute cores and {gib(report.host.memory_gb - report.ceiling.memory_gb)} for TWS, risk supervision, Redis and local inference. Research uses one native thread per worker; dashboard research also uses lower scheduling priority.</p>
      <p>{report.measurement}. Research exceeding its reservation stops at the next memory sample. Short peaks between samples may be missed.</p>
      <ul>{report.requests.map((row) => {
        const job = jobs.find(item => item.id === row.label);
        return <li key={row.token}><strong>{job?.name || row.label}</strong><ResourceReading resources={row} />
          {job && row.state === "queued" && <button type="button" disabled={busy} onClick={() => cancel(job.id)}>Cancel queued {job.name || job.id}</button>}
          {!job && row.state === "queued" && <span>Cancel this CLI request with Ctrl-C in its terminal.</span>}
        </li>;
      })}</ul>
      {!report.requests.length && <p>No research reservations.</p>}
      <details><summary>Completed resource measurements</summary>
        {jobs.filter(job => job.resources && ["completed", "failed", "cancelled"].includes(job.status)).map(job =>
          <div key={job.id}><strong>{job.name || job.id} · {job.status}</strong><ResourceReading resources={{ ...job.resources, state: "finished" }} /></div>)}
        {!jobs.some(job => job.resources && ["completed", "failed", "cancelled"].includes(job.status)) && <p>No completed measurements recorded.</p>}
      </details>
      <form onSubmit={save}>
        <p>Limits apply across dashboard and CLI research. Current limits: {report.limits.cpu_slots} slots, {gib(report.limits.memory_gb)}. Cancel incompatible reservations before reducing limits.</p>
        <label>Research CPU slots<input type="number" min="1" max={report.ceiling.cpu_slots} required value={cpu} onChange={event => { setCpu(event.target.value); setSaved(false); }} /></label>
        <label>Research memory GiB<input type="number" min="0.1" step="0.1" max={report.ceiling.memory_gb} required value={memory} onChange={event => { setMemory(event.target.value); setSaved(false); }} /></label>
        <label>Host capacity operator token<input type="password" autoComplete="off" required value={token} onChange={event => setToken(event.target.value)} /></label>
        <p>Use the locally configured QUANT_CONTROL_TOKEN. The token stays in this panel’s memory.</p>
        <button type="submit" disabled={busy || !token || !cpu || !memory}>{busy ? "Saving…" : "Save host limits"}</button>
        {saved && <p role="status">Host limits saved.</p>}
      </form>
    </>}
  </details>;
}
