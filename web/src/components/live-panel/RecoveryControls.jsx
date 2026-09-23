// THESIS: Recovery evidence belongs beside the operator's safety controls.
// OWN-WORLD: Existing instrument housing, neutral controls and text statuses.
// STORY: Configure a destination, protect state, then inspect an isolated restore.
// FIRST VIEWPORT: Backup age and unmet validation above configuration and launch.
// FORM: Local extension of the existing disclosure; no separate visual world.
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api.js";
import "./safety-controls.css";

const DEFAULTS = { repository: "", password_file: "", retain: 56, rpo_hours: 6, rto_minutes: 120, scheduled: false, redis_host: "127.0.0.1", redis_port: 6379 };
const ACTIVE = ["starting", "running", "cancelling"];

export default function RecoveryControls() {
  const [token, setToken] = useState("");
  const [credential, setCredential] = useState("");
  const [state, setState] = useState(null);
  const [config, setConfig] = useState(DEFAULTS);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState("backup");
  const [snapshot, setSnapshot] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [retry, setRetry] = useState(null);
  const pending = useRef(false);
  const initialized = useRef(false);

  useEffect(() => {
    if (!credential) return;
    let stopped = false;
    let timer;
    async function poll() {
      try {
        const next = await api.getRecovery(credential);
        if (stopped) return;
        if (!Array.isArray(next.reports) || !next.dependencies || !next.as_of) throw new Error("Recovery status is incomplete");
        setState(next); setPollError("");
        if (!initialized.current) {
          setConfig(next.config || DEFAULTS); initialized.current = true; setLoaded(true);
        }
      } catch (err) { if (!stopped) setPollError(err.message); }
      finally { if (!stopped) timer = setTimeout(poll, 3000); }
    }
    poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [credential]);

  const active = state?.reports?.some((r) => ACTIVE.includes(r.status));
  const dirty = Object.keys(DEFAULTS).some((key) => config[key] !== (state?.config || DEFAULTS)[key]);
  const expected = action === "restore" ? `RESTORE ISOLATED ${snapshot}` : action === "initialize" ? `INITIALIZE ${config.repository}` : "";
  const unavailable = !state || !!pollError || busy || active || !!retry;
  const missing = Object.entries(state?.dependencies || {}).filter(([, available]) => !available).map(([name]) => name);

  async function execute(fn) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError("");
    try { await fn(); }
    catch (err) { setError(err.message); }
    finally { pending.current = false; setBusy(false); }
  }

  async function send(body) {
    await execute(async () => {
      try {
        const job = await api.launchRecovery(credential, body);
        setRetry(null); setConfirmation("");
        setState((old) => ({ ...old, reports: [{ id: body.request_id.replaceAll("-", ""), action: body.action, status: job.status, job_id: job.id }, ...old.reports.filter((r) => r.job_id !== job.id)] }));
      } catch (err) {
        if (!err.status || err.status >= 500) setRetry(body);
        throw err;
      }
    });
  }

  return <details className="safety-controls recovery-controls">
    <summary>Backup &amp; recovery</summary>
    <p>Encrypted off-host copies of research, audit, allocation and kill-switch state. An off-host destination and a clean-host restore drill are still required.</p>
    {!credential ? <form onSubmit={(event) => { event.preventDefault(); initialized.current = false; setCredential(token); setToken(""); }}>
      <label>Recovery operator token<input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} required /></label>
      <button disabled={!token}>Unlock recovery controls</button>
    </form> : <>
      <div className="safety-controls__status" role="status"><span>{pollError ? "Status unknown — controls locked" : state ? `Backup age: ${state.backup_age_hours == null ? "unavailable" : `${state.backup_age_hours.toFixed(1)} hours`} · ${state.rpo_status}` : "Loading recovery status…"}</span>
        <button disabled={busy} onClick={() => { setCredential(""); setState(null); setLoaded(false); setRetry(null); setError(""); setPollError(""); }}>Lock controls</button></div>
      {pollError && <p role="alert">{pollError}. Unsaved configuration is retained.</p>}
      {missing.length > 0 && <p>Install on the API host: {missing.join(", ")}. Configuration can be saved before installation.</p>}
      {loaded && <form onSubmit={(event) => { event.preventDefault(); execute(async () => {
        const saved = await api.saveRecovery(credential, config);
        setState((old) => ({ ...old, config: saved })); setConfig(saved);
      }); }}>
        <label>SFTP repository<input value={config.repository} placeholder="sftp:backup@host:/backups/quant" onChange={(e) => setConfig({ ...config, repository: e.target.value })} required /></label>
        <label>Repository password file on API host<input value={config.password_file} placeholder="/absolute/private/path/restic-password" onChange={(e) => setConfig({ ...config, password_file: e.target.value })} required /></label>
        <label>Execution Redis host<input value={config.redis_host} onChange={(e) => setConfig({ ...config, redis_host: e.target.value })} required /></label>
        <label>Execution Redis port<input type="number" min="1" max="65535" value={config.redis_port} onChange={(e) => setConfig({ ...config, redis_port: Number(e.target.value) })} required /></label>
        <p>Use an owner-only password file outside this project. SSH credentials and a verified host key must already be configured on the API host. Keep a separate recovery copy of the password.</p>
        <label>Snapshots to retain<input type="number" min="2" max="1000" value={config.retain} onChange={(e) => setConfig({ ...config, retain: Number(e.target.value) })} required /></label>
        <label>Maximum data-loss target (hours)<input type="number" min="1" max="168" value={config.rpo_hours} onChange={(e) => setConfig({ ...config, rpo_hours: Number(e.target.value) })} required /></label>
        <label>Recovery-time target (minutes)<input type="number" min="1" max="10080" value={config.rto_minutes} onChange={(e) => setConfig({ ...config, rto_minutes: Number(e.target.value) })} required /></label>
        <label>Automatic backups<select value={String(config.scheduled)} onChange={(e) => setConfig({ ...config, scheduled: e.target.value === "true" })}><option value="false">Off — manual launch</option><option value="true">On — while API is running</option></select></label>
        <p>Successful backups apply retention only to Quant recovery snapshots. Scheduling runs at the data-loss target interval; failures retry hourly. API downtime can exceed that target.</p>
        <button disabled={unavailable || !dirty}>Save recovery configuration</button>
      </form>}
      <form onSubmit={(event) => { event.preventDefault(); send({ request_id: crypto.randomUUID(), action, snapshot: action === "restore" ? snapshot : null, confirmation }); }}>
        <label>Recovery action<select value={action} onChange={(e) => { setAction(e.target.value); setConfirmation(""); }} disabled={busy || !!retry}>
          <option value="backup">Back up and verify</option><option value="check">Check remote integrity / refresh snapshots</option><option value="restore">Restore isolated copy</option><option value="initialize">Initialize new encrypted repository</option>
        </select></label>
        {action === "restore" && <label>Remote snapshot<select value={snapshot} onChange={(e) => { setSnapshot(e.target.value); setConfirmation(""); }} required><option value="">Select a verified inventory entry</option>{(state?.inventory?.snapshots || []).map((s) => <option key={s.id} value={s.id}>{s.time} · {s.id.slice(0, 12)}</option>)}</select></label>}
        {action === "restore" && <p>Restore writes a new isolated directory on this API host. It never overwrites running state, connects to IBKR, or enables trading. After an outage, use a clean host and reconcile before resuming.</p>}
        {expected && <label>Type <code>{expected}</code><input value={confirmation} autoComplete="off" onChange={(e) => setConfirmation(e.target.value)} /></label>}
        {dirty && <p>Save configuration before launching.</p>}
        <button disabled={unavailable || dirty || !state?.config || missing.length > 0 || confirmation !== expected || (action === "restore" && !snapshot)}>Launch recovery job</button>
      </form>
      {retry && <div><p>Receipt is uncertain. Retry the same request to avoid a duplicate job.</p><button disabled={busy || !!pollError} onClick={() => send(retry)}>Retry same recovery request</button></div>}
      {error && <p role="alert">{error}</p>}
      <ol className="safety-controls__history" aria-label="Recovery job history">{(state?.reports || []).map((report) => <li key={report.id}>
        <div role="status">{report.action} · {report.status} · {report.phase || "Waiting for worker"}</div>
        {report.snapshot && <code>{report.snapshot}</code>}
        {report.error && <p>{report.error}</p>}
        {report.restore_path && <><p>Restored copy: <code>{report.restore_path}</code></p><p>{report.different_hostname ? "Different hostname reported; clean-host validation pending" : "Same-host drill only"} · Broker reconciliation required · Execution remains blocked</p></>}
        {ACTIVE.includes(report.status) && <button disabled={busy || !!pollError || report.status === "cancelling"} onClick={() => execute(async () => {
          await api.cancelRecovery(credential, report.id);
          setState((old) => ({ ...old, reports: old.reports.map((r) => r.id === report.id ? { ...r, status: "cancelling" } : r) }));
        })}>Cancel recovery job</button>}
        <details><summary>Review recovery evidence</summary><pre>{JSON.stringify(report, null, 2)}</pre></details>
      </li>)}</ol>
      {state && !state.reports.length && <p>No recovery jobs recorded. Initialize a new destination, or check an existing repository to load its snapshots.</p>}
    </>}
  </details>;
}
