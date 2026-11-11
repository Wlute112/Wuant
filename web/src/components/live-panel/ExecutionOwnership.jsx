// Preserve the instrument deck: one read-only ownership disclosure connects
// account/session configuration, launch conflicts, running status and shutdown
// review. The existing session stop path is the only release control.
import { useCallback, useEffect, useRef, useState } from "react";
import { useInterval } from "../../hooks/useInterval.js";
import { api } from "../../lib/api.js";
import { isJobActive } from "../../lib/jobs.js";
import { ownershipIsFresh } from "../../lib/executionOwnership.js";
import "./safety-controls.css";

export default function ExecutionOwnership({ jobs = [], onJobUpdated }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [receivedAt, setReceivedAt] = useState(null);
  const [now, setNow] = useState(Date.now());
  const pending = useRef(false);
  const load = useCallback(async () => {
    if (pending.current) return;
    pending.current = true;
    try {
      const next = await api.getExecutionOwnership();
      const received = Date.now();
      setReport(next); setReceivedAt(received); setNow(received); setError(null);
    }
    catch (cause) { setError(`Execution ownership is unknown: ${cause.message}. Last reported details may be stale.`); }
    finally { pending.current = false; }
  }, []);
  useEffect(() => { load(); }, [load]);
  useInterval(load, 3000);
  useInterval(() => setNow(Date.now()), 1000);
  const unknown = Boolean(error) || !ownershipIsFresh(report, receivedAt, now);
  const owner = report?.owner;
  const job = jobs.find(item => `strategy:${item.id}` === owner?.owner_id);
  const stop = async () => {
    setStopping(true); setActionError(null);
    try { onJobUpdated?.(await api.cancelJob(job.id)); await load(); }
    catch (cause) { setActionError(`Could not stop the owner: ${cause.message}`); }
    finally { setStopping(false); }
  };
  return <details className="safety-controls execution-ownership">
    <summary>Broker execution ownership · {unknown ? "UNKNOWN" : report.status}</summary>
    <p role="status">{report?.policy || "One local broker runner across every account, profile and client ID. Ownership is enforced automatically before broker startup."}</p>
    {error && <p role="alert">{error}</p>}
    {report && unknown && !error && <p role="status">Ownership reading expired or has an invalid timestamp. Current ownership is unknown; last reported details below are historical.</p>}
    {actionError && <p role="alert">{actionError}</p>}
    {(error || report && unknown) && <button type="button" onClick={load}>Retry ownership status</button>}
    {owner && <p>{unknown ? "Last reported owner" : "Owner"}: <strong>{job?.name || owner.owner_id || "Identity unavailable"}</strong><br />
      {owner.mode} · {owner.asset_class} · client {owner.client_id} · process {owner.pid}<br />
      Acquired {owner.acquired_at || "time unavailable"}</p>}
    {!unknown && report && !report.active && <p>No local runner currently holds the execution slot. This does not prove broker orders or positions are flat; startup still requires reconciliation.</p>}
    {job && isJobActive(job) && <button type="button" disabled={stopping || job.status === "cancelling" || unknown} onClick={stop}>
      {stopping || job.status === "cancelling" ? "Stopping owner…" : "Stop owning session"}
    </button>}
    {owner && !job && <p>This runner was started outside the dashboard registry. Stop it with Ctrl-C in its terminal; ownership releases only after verified disposal or process exit.</p>}
    {owner && job && !isJobActive(job) && <p>The job is recorded as terminal, but its process still holds ownership. Wait for disposal; if ownership remains held, inspect the owning process before restarting.</p>}
    <p>Paused runners retain ownership. There is no force-unlock control. Configure the account in Session controls, start once, and review the owner here. Stop the owner before launching another account or profile.</p>
    <p>{report?.limitations || "This guard covers the local OS user, not other hosts or broker clients outside this application."}</p>
  </details>;
}
