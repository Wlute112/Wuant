import { useState } from "react";
import { api } from "../../lib/api.js";

export default function CorporateActions({ jobId, token, state }) {
  const [kind, setKind] = useState("SPLIT");
  const [conId, setConId] = useState("");
  const [eventId, setEventId] = useState("");
  const [effective, setEffective] = useState("");
  const [source, setSource] = useState("");
  const [value, setValue] = useState("");
  const [newConId, setNewConId] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const expected = `RECORD CORPORATE ACTION ${jobId}`;

  async function submit(event) {
    event.preventDefault();
    if (busy || !state?.available || confirmation !== expected) return;
    setBusy(true); setMessage("");
    try {
      const payload = { event_id: eventId, kind, con_id: Number(conId),
        effective_at: effective, source_reference: source, currency: "USD" };
      if (kind === "SPLIT") payload.split_ratio = value;
      if (kind === "CASH_DIVIDEND") payload.cash_per_share = value;
      if (kind === "SYMBOL_CHANGE") {
        payload.new_symbol = value.trim().toUpperCase();
        if (newConId) payload.new_con_id = Number(newConId);
      }
      const result = await api.recordCorporateAction(jobId, token, { event: payload, confirmation });
      setMessage(`${result.event_id}: ${result.status}. Review the recovery steps below.`);
      setConfirmation("");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  async function cancel(eventId) {
    setBusy(true);
    try {
      await api.cancelCorporateAction(jobId, token, eventId);
      setMessage("Event cancelled. Any operator freeze remains in place until a guarded resume.");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  return <details className="corporate-actions">
    <summary>Corporate actions & instrument identity</summary>
    {!state?.available ? <p>{state?.reason || "Identity status unknown. Connect a paper equity session to qualify instruments."}</p> : <>
      <p>Broker-qualified identities · {state.account_id}</p>
      <div className="corporate-actions__table"><table><caption>US-listed USD instruments</caption><thead><tr><th>conId</th><th>Symbol</th><th>Primary exchange</th><th>Stock type</th><th>Known aliases</th></tr></thead>
        <tbody>{state.identities.map((item) => <tr key={item.con_id}><td>{item.con_id}</td><td>{item.symbol}</td><td>{item.primary_exchange}</td><td>{item.stock_type}</td><td>{item.aliases.join(", ")}</td></tr>)}</tbody></table></div>
      <p>Record a reviewed broker or issuer notice. This is an operator-maintained event journal; it does not discover corporate actions automatically.</p>
      <form onSubmit={submit}>
        <label>Action<select value={kind} disabled={busy} onChange={(event) => { setKind(event.target.value); setValue(""); setNewConId(""); }}><option value="SPLIT">Split / reverse split</option><option value="CASH_DIVIDEND">Cash dividend</option><option value="SYMBOL_CHANGE">Symbol change</option></select></label>
        <label>Qualified instrument<select value={conId} onChange={(event) => setConId(event.target.value)} required disabled={busy}><option value="">Select a conId</option>{state.identities.map((item) => <option key={item.con_id} value={item.con_id}>{item.symbol} · {item.con_id}</option>)}</select></label>
        <label>Broker / issuer event ID<input value={eventId} required maxLength={100} pattern="[A-Za-z0-9_.:-]+" disabled={busy} onChange={(event) => setEventId(event.target.value)} /></label>
        <label>Effective instant (include timezone)<input value={effective} placeholder="2026-09-08T09:30:00-04:00" required disabled={busy} onChange={(event) => setEffective(event.target.value)} /></label>
        <label>Source reference<input value={source} minLength={5} maxLength={500} placeholder="Broker statement ID or issuer notice URL" required disabled={busy} onChange={(event) => setSource(event.target.value)} /></label>
        <label>{kind === "SPLIT" ? "New shares / old shares (2 = 2-for-1; 0.1 = 1-for-10)" : kind === "CASH_DIVIDEND" ? "Cash per share (USD)" : "New symbol"}<input value={value} required disabled={busy} onChange={(event) => setValue(event.target.value)} /></label>
        {kind === "SYMBOL_CHANGE" && <label>Replacement conId (only if the notice changes it)<input type="number" min="1" step="1" value={newConId} disabled={busy} onChange={(event) => setNewConId(event.target.value)} /></label>}
        <p>Recording an event freezes entries when the strategy next checks its control journal. Then flatten and stop the session using the controls above. After the effective instant, restart with the affected instrument included. Old aliases resolve through the registry; the broker must confirm the new identity.</p>
        <p>Recovery requires no broker positions or working orders. Model history is refetched; dividend cash and position quantities come only from the broker. Resume remains blocked through fresh model warmup.</p>
        <label>Type <code>{expected}</code><input value={confirmation} autoComplete="off" disabled={busy} onChange={(event) => setConfirmation(event.target.value)} /></label>
        <button type="submit" disabled={busy || confirmation !== expected}>{busy ? "Recording…" : "Record reviewed event"}</button>
      </form>
      {message && <p role="status">{message}</p>}
      <h4>Recovery journal</h4>
      {!state.events.length && <p>No corporate actions recorded for this account.</p>}
      <ol className="safety-controls__history">{state.events.map((event) => <li key={event.event_id}>
        <strong>{event.kind} · {event.event_id} · {event.status}</strong>
        <span>conId {event.con_id} · effective {event.effective_at}</span>
        <span>{event.source_reference} · recorded by {event.operator}</span>
        {event.status === "PENDING" && <><span>Flatten, stop and restart after the effective instant.</span><button type="button" disabled={busy} onClick={() => cancel(event.event_id)}>Cancel pending event</button></>}
        {event.status === "REBUILDING" && <span>Broker identity and flat account confirmed. Waiting for fresh model warmup.</span>}
        {event.status === "APPLIED" && <span>Fresh model warmup completed. Review telemetry before guarded resume.</span>}
      </li>)}</ol>
    </>}
  </details>;
}
