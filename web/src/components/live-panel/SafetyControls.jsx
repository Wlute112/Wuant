// Extend the instrument deck: identity and health precede actions; durable
// command results remain visible until reviewed. Credentials stay in memory.
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api.js";
import { operationResult, validOperationsSnapshot } from "../../lib/operations.js";
import CorporateActions from "./CorporateActions.jsx";
import "./safety-controls.css";

const LABELS = {
  FREEZE_ENTRIES: "Freeze entries",
  RESUME_ENTRIES: "Resume entries",
  CANCEL_ALL: "Cancel orders · flat book only",
  FLATTEN: "Flatten positions",
  KILL: "Engage permanent kill",
};
const DETAILS = {
  FREEZE_ENTRIES: "Freeze new entries and request cancellation of resting entries. Position protection stays active.",
  RESUME_ENTRIES: "Release an operator freeze only after the strategy checks broker, risk, data, session and supervisor health.",
  CANCEL_ALL: "Freeze entries and cancel this strategy’s orders only if it has no open positions. A racing fill requires review.",
  FLATTEN: "Freeze entries, wait for order cancellations, then request exits. Completion requires broker confirmation that the book is flat.",
  KILL: "Permanently disable automated execution and request a confirmed flatten. Resume cannot undo the kill-switch.",
};

export default function SafetyControls({ job, isDemo }) {
  const [token, setToken] = useState("");
  const [credential, setCredential] = useState("");
  const [state, setState] = useState(null);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState("FREEZE_ENTRIES");
  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [retry, setRetry] = useState(null);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const jobId = job?.id;

  useEffect(() => {
    const current = ++generation.current;
    setState(null);
    setError("");
    setPollError("");
    setRetry(null);
    if (!credential || !jobId) return;
    let disposed = false;
    let timer;
    async function poll() {
      try {
        const result = await api.getOperations(jobId, credential);
        if (disposed || generation.current !== current) return;
        if (!validOperationsSnapshot(result, jobId)) throw new Error("Control response is incomplete or belongs to another session. Controls remain locked.");
        setState(result);
        setPollError("");
      } catch (err) {
        if (disposed || generation.current !== current) return;
        setState(null);
        setPollError(err.message);
      } finally {
        if (!disposed) timer = setTimeout(poll, 2000);
      }
    }
    poll();
    return () => { disposed = true; clearTimeout(timer); generation.current++; };
  }, [jobId, credential]);

  useEffect(() => {
    setCredential(""); setToken(""); setReason(""); setConfirmation("");
  }, [jobId]);

  const expected = `${action} strategy:${jobId}`;
  const resumeBlocked = action === "RESUME_ENTRIES" && (!state || !state.heartbeat_fresh || state.resume_blockers.length > 0);
  const canSubmit = state && job?.status === "running" && !resumeBlocked
    && reason.trim().length >= 3 && (action === "FREEZE_ENTRIES" || confirmation === expected);

  async function send(body) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError("");
    const current = generation.current;
    try {
      const result = await api.submitOperation(jobId, credential, body);
      if (current !== generation.current) return;
      setRetry(null); setConfirmation("");
      setState((old) => old ? { ...old, commands: [result, ...old.commands.filter((item) => item.command_id !== result.command_id)] } : old);
    } catch (err) {
      if (current !== generation.current) return;
      setError(`${err.message}${err.detail?.blockers ? `: ${err.detail.blockers.join(" ")}` : ""}`);
      // Preserve the exact request ID/content when receipt is uncertain.
      if (!err.status || err.status >= 500) setRetry(body);
      else setRetry(null);
    } finally { inFlight.current = false; setBusy(false); }
  }

  async function cancel(commandId) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true);
    const current = generation.current;
    try {
      const result = await api.cancelOperation(jobId, credential, commandId);
      if (current !== generation.current) return;
      setState((old) => old ? { ...old, commands: old.commands.map((item) => item.command_id === commandId ? result : item) } : old);
    } catch (err) {
      if (current === generation.current) setError(err.message);
    } finally { inFlight.current = false; setBusy(false); }
  }

  return <details className="safety-controls">
    <summary>Operator safety controls</summary>
    {isDemo || !jobId || job?.kind !== "paper" ? <p>Available for registered paper sessions. Live capital remains locked.</p> : <>
      <p className="num">Target: {jobId}</p>
      {!credential ? <form onSubmit={(event) => { event.preventDefault(); setCredential(token); setToken(""); }}>
        <label>Operator token<input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} required /></label>
        <p>Use the token configured as QUANT_CONTROL_TOKEN on the API server. It is held only in this panel’s memory.</p>
        <button type="submit" disabled={!token}>Unlock controls</button>
      </form> : <>
        <div className="safety-controls__status" role="status">
          <span>{state ? `${state.operator} · ${state.heartbeat_fresh ? "strategy responding" : "strategy heartbeat stale"}` : "Control status unknown"}</span>
          <button type="button" onClick={() => { setCredential(""); setToken(""); }}>Lock</button>
        </div>
        {error && <p role="alert">{error}</p>}
        {pollError && <p role="alert">{pollError}</p>}
        {state?.resume_blockers?.length > 0 && <div><p>Resume interlocks</p><ul>{state.resume_blockers.map((message) => <li key={message}>{message}</li>)}</ul></div>}
        {!state?.heartbeat_fresh && <p>Safety requests may remain pending until the strategy responds. A queued command does not confirm a broker action.</p>}
        {retry ? <div role="alert"><p>Command receipt is uncertain. Retry the same request to recover its status without duplicating it.</p><button type="button" disabled={busy} onClick={() => send(retry)}>Retry request</button></div> : <form onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) send({ action, reason: reason.trim(), confirmation, request_id: crypto.randomUUID() });
        }}>
          <label>Action<select value={action} disabled={busy} onChange={(event) => { setAction(event.target.value); setConfirmation(""); }}>
            {Object.entries(LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
          <p>{DETAILS[action]}</p>
          <label>Reason<textarea value={reason} maxLength={500} minLength={3} required disabled={busy} onChange={(event) => setReason(event.target.value)} /></label>
          {action !== "FREEZE_ENTRIES" && <label>Type <code>{expected}</code><input value={confirmation} autoComplete="off" disabled={busy} onChange={(event) => setConfirmation(event.target.value)} /></label>}
          <button type="submit" disabled={busy || !canSubmit}>{busy ? "Submitting…" : LABELS[action]}</button>
        </form>}
        <h4>Command history</h4>
        <CorporateActions key={jobId} jobId={jobId} token={credential} state={state?.corporate_actions} />
        {state && !state.commands.length && <p>No commands recorded for this session.</p>}
        <ol className="safety-controls__history">{state?.commands.map((command) => <li key={command.command_id}>
          <strong>{LABELS[command.action] || command.action} · {command.status}</strong>
          <span>{new Date(command.requested_at).toLocaleString()} · {command.payload?.operator || "supervisor"}</span>
          <span>{command.reason}</span>
          <span>{operationResult(command)}</span>
          {command.status === "PENDING" && <button type="button" disabled={busy} onClick={() => cancel(command.command_id)}>Cancel pending request</button>}
        </li>)}</ol>
      </>}
    </>}
  </details>;
}
