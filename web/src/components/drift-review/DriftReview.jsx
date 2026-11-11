import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api.js";
import "./drift-review.css";

const METRICS = {
  prediction_mean: ["Prediction shift (log return)", 0.002],
  absolute_error: ["Mean absolute error (log return)", 0.002],
  direction_accuracy: ["Direction accuracy drop (fraction)", 0.1],
  slippage_bps: ["Adverse slippage increase (bps)", 5],
  reject_rate: ["Reject rate increase (fraction)", 0.05],
  feature_age_seconds: ["Peer input age increase (seconds)", 3600],
};
const DEFAULTS = { baseline_id: "", reason: "", minimum_samples: 30, window_samples: 1000,
  stale_after_seconds: 300, enabled: true, thresholds: Object.fromEntries(Object.entries(METRICS).map(([key, value]) => [key, value[1]])) };
const configFields = (config) => Object.fromEntries(Object.keys(DEFAULTS).map(key => [key, config[key]]));
const number = value => value == null ? "Unavailable" : new Intl.NumberFormat(undefined, { maximumSignificantDigits: 5 }).format(value);
const statusText = value => String(value || "unknown").replaceAll("_", " ");

export default function DriftReview({ runs = [], jobs = [], navigation, brokerStatus, theme, onThemeChange }) {
  const [token, setToken] = useState("");
  const [credential, setCredential] = useState("");
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState(DEFAULTS);
  const [source, setSource] = useState("");
  const [reason, setReason] = useState("");
  const [runId, setRunId] = useState("");
  const [jobId, setJobId] = useState("");
  const [evidence, setEvidence] = useState("");
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [reportId, setReportId] = useState("");
  const pending = useRef(false);
  const generation = useRef(0);
  const initialized = useRef(false);

  useEffect(() => () => { generation.current += 1; }, []);

  useEffect(() => {
    if (!credential) return undefined;
    let stopped = false;
    let timer;
    async function poll() {
      try {
        if (pending.current) return;
        const next = await api.getDrift(credential);
        if (stopped || pending.current) return;
        if (!Array.isArray(next.sources) || !Array.isArray(next.reports) || !Array.isArray(next.baselines) || !Array.isArray(next.alerts)) throw new Error("Review status is incomplete");
        setState(next); setPollError("");
        if (!initialized.current) {
          if (next.config) setDraft(configFields(next.config));
          initialized.current = true;
        }
      } catch (err) { if (!stopped) setPollError(err.message); }
      finally { if (!stopped) timer = setTimeout(poll, 3000); }
    }
    poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [credential]);

  async function execute(action, success) {
    if (pending.current) return;
    const current = generation.current;
    pending.current = true; setBusy(true); setError(""); setMessage("");
    try {
      const result = await action();
      if (current !== generation.current) return;
      success?.(result);
      const next = await api.getDrift(credential);
      if (current === generation.current) { setState(next); setPollError(""); }
    } catch (err) { if (current === generation.current) setError(err.message); }
    finally { if (current === generation.current) { pending.current = false; setBusy(false); } }
  }

  const locked = busy || !state || !!pollError;
  const dirty = !!state?.config && JSON.stringify(configFields(state.config)) !== JSON.stringify(draft);
  const latest = state?.reports[0];
  const report = state?.reports.find(r => r.id === reportId) || latest;
  const historical = report && report.config_id !== state.config?.id;
  const latestHistorical = latest && latest.config_id !== state.config?.id;
  const overdue = state?.config?.enabled && latest && Date.now() - Date.parse(latest.recorded_at) > 60000;

  return <section className="drift-review" aria-labelledby="drift-review-title">
    <header className="drift-review__toolbar">
      {navigation}<h1 id="drift-review-title">Drift &amp; execution review</h1>
      <label>Theme<select aria-label="Theme" value={theme} onChange={e => onThemeChange(e.target.value)}><option value="dark">Dark</option><option value="light">White</option><option value="lilac">Lilac</option></select></label>
      {brokerStatus}
    </header>
    <p className="drift-review__intro">Compare research, shadow and paper evidence against a declared model version. Alerts request review; model and parameter changes require a new reviewed version.</p>
    {!credential ? <form className="drift-review__unlock" onSubmit={e => { e.preventDefault(); initialized.current = false; setCredential(token); setToken(""); }}>
      <label>Review operator token<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} required /></label>
      <button disabled={!token}>Unlock drift review</button>
    </form> : <>
      <div className="drift-review__status" role="status">
        <span>{pollError ? "Status unknown — review controls locked" : !state ? "Loading review evidence…" : state.config?.enabled ? "Automatic review enabled" : "Automatic review paused"}</span>
        <button disabled={busy} onClick={() => { generation.current += 1; setCredential(""); setState(null); setError(""); setPollError(""); setMessage(""); setEvidence(""); setDraft(DEFAULTS); }}>Lock review</button>
      </div>
      {state && <nav className="drift-review__actions" aria-label="Drift review sections">
        <a href="#drift-results-title">Comparison results</a><a href="#drift-evidence-title">Evidence sources</a>
        <a href="#drift-settings-title">Review settings</a><a href="#drift-alerts-title">Alert history</a>
      </nav>}
      {state && <p role="status">{!latest ? "No review has run yet." : latestHistorical ? "Current settings await review; previous results are historical." : `Last recorded review: ${latest.recorded_at}`}</p>}
      {overdue && <p role="alert">Automatic review is overdue. Results may be stale; check the API process or run a review manually.</p>}
      {pollError && <p role="alert">{pollError}. Unsaved configuration is retained; status retries automatically.</p>}
      {error && <p role="alert">{error}</p>}
      <p role="status" aria-live="polite">{busy ? "Saving or collecting review evidence…" : message}</p>
      {state && <>
        <section className="drift-review__section" aria-labelledby="drift-evidence-title">
          <h2 id="drift-evidence-title">Evidence sources</h2>
          <p>Use a fresh backtest or paper session with a model fingerprint. Shadow evidence is imported from a recorded run; demonstration data is excluded.</p>
          <div className="drift-review__forms">
            <form onSubmit={e => { e.preventDefault(); execute(() => api.importDriftResearch(credential, runId), () => setMessage("Backtest evidence imported.")); }}>
              <label>Backtest run<select aria-label="Backtest run" value={runId} onChange={e => setRunId(e.target.value)} required><option value="">Choose a backtest</option>{runs.filter(r => r.kind === "backtest").map(r => <option key={r.run_id} value={r.run_id}>{r.name || r.run_id} · {r.asset_class}</option>)}</select></label>
              <button disabled={locked || !runId}>Import research evidence</button>
            </form>
            <form onSubmit={e => { e.preventDefault(); execute(() => api.importDriftPaper(credential, jobId), () => setMessage("Paper evidence collected.")); }}>
              <label>Paper session<select aria-label="Paper session" value={jobId} onChange={e => setJobId(e.target.value)} required><option value="">Choose a session</option>{jobs.filter(j => j.kind === "paper").map(j => <option key={j.id} value={j.id}>{j.name || j.id} · {j.status}</option>)}</select></label>
              <button disabled={locked || !jobId}>Collect paper evidence</button>
            </form>
          </div>
          <details><summary>Import recorded evidence JSON</summary>
            <p>Export an existing source to inspect the schema. Use its exact contract for a recorded shadow run, a unique source_id, dated source reference and observed samples. Actual returns require a later target_ts. Imports retain the supplied provenance; they do not certify market accuracy.</p>
            <form onSubmit={e => { e.preventDefault(); execute(() => api.importDriftEvidence(credential, JSON.parse(evidence)), () => { setEvidence(""); setMessage("Recorded evidence imported."); }); }}>
              <label>Evidence JSON<textarea rows={8} value={evidence} onChange={e => setEvidence(e.target.value)} required spellCheck="false" /></label>
              <button disabled={locked || !evidence.trim()}>Import recorded evidence</button>
            </form>
          </details>
          {!state.sources.length ? <p>No evidence imported. Start with a fresh backtest or a recorded paper/shadow source.</p> : <ul className="drift-review__sources">{state.sources.map(s => <li key={s.source_id}>
            <strong>{s.source_id}</strong><span>{s.kind} · {s.contract.asset_class} · {s.sample_count} observations · {s.as_of}</span>
            <code>Model {s.contract.model_version}</code><p>{s.reference}</p>
            {s.provenance && <details><summary>Dataset and execution provenance</summary><pre>{JSON.stringify(s.provenance, null, 2)}</pre></details>}
            <button disabled={locked} onClick={() => execute(() => api.exportDriftEvidence(credential, s.source_id), result => {
              const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
              const link = document.createElement("a"); link.href = url; link.download = `${s.source_id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
            })}>Export {s.source_id}</button>
          </li>)}</ul>}
        </section>

        <section className="drift-review__section" aria-labelledby="drift-baseline-title">
          <h2 id="drift-baseline-title">Declared baseline</h2>
          <p>Capture an immutable source snapshot with a review reason. Changes to the alpha, universe, bar basis or source code produce a different model fingerprint.</p>
          <form onSubmit={e => { e.preventDefault(); execute(() => api.declareDriftBaseline(credential, { source_id: source, reason }), result => {
            setDraft(d => ({ ...d, baseline_id: result.id })); setMessage("Baseline captured. Save review settings to use it.");
          }); }}>
            <div className="drift-review__forms">
              <label>Baseline source<select aria-label="Baseline source" required value={source} onChange={e => setSource(e.target.value)}><option value="">Choose imported evidence</option>{state.sources.map(s => <option key={s.source_id} value={s.source_id}>{s.source_id} · {s.kind}</option>)}</select></label>
              <label>Baseline review reason<input value={reason} minLength={5} maxLength={500} onChange={e => setReason(e.target.value)} required /></label>
            </div>
            <button disabled={locked || !source}>Capture baseline</button>
          </form>
          {state.baselines.length > 0 && <details><summary>Baseline review history</summary><ul>{state.baselines.map(b => <li key={b.id}>
            <strong>{b.source_id}</strong> · {b.recorded_at} · {b.operator}<p>{b.reason}</p><code>SHA-256 {b.sha256}</code>
          </li>)}</ul></details>}
        </section>

        <section className="drift-review__section" aria-labelledby="drift-settings-title">
          <h2 id="drift-settings-title">Review settings</h2>
          <form onSubmit={e => { e.preventDefault(); execute(() => api.configureDrift(credential, draft), result => { setDraft(configFields(result)); setMessage("Versioned review settings saved."); }); }}>
            <div className="drift-review__forms">
              <label>Reviewed baseline<select aria-label="Reviewed baseline" value={draft.baseline_id} onChange={e => setDraft({ ...draft, baseline_id: e.target.value })} required><option value="">Choose a baseline</option>{state.baselines.map(b => <option key={b.id} value={b.id}>{b.source_id} · {b.recorded_at}</option>)}</select></label>
              <label>Settings review reason<input value={draft.reason} minLength={5} maxLength={500} onChange={e => setDraft({ ...draft, reason: e.target.value })} required /></label>
              {[["minimum_samples", "Minimum samples per metric", 2, 10000], ["window_samples", "Observation window per source", 2, 10000], ["stale_after_seconds", "Paper / shadow freshness limit (seconds)", 15, 604800]].map(([key, label, min, max]) => <label key={key}>{label}<input required type="number" min={min} max={max} value={draft[key]} onChange={e => setDraft({ ...draft, [key]: Number(e.target.value) })} /></label>)}
              <label>Automatic review<select aria-label="Automatic review" value={String(draft.enabled)} onChange={e => setDraft({ ...draft, enabled: e.target.value === "true" })}><option value="true">Enabled</option><option value="false">Paused</option></select></label>
            </div>
            <fieldset><legend>Adverse change thresholds</legend><p>Prediction shift uses an absolute change. Accuracy alerts on a decrease; all other metrics alert on an increase. A breach requires enough observations in both sources.</p>
              <div className="drift-review__forms">{Object.entries(METRICS).map(([key, [label]]) => <label key={key}>{label}<input required type="number" min="0" step="any" value={draft.thresholds[key]} onChange={e => setDraft({ ...draft, thresholds: { ...draft.thresholds, [key]: Number(e.target.value) } })} /></label>)}</div>
            </fieldset>
            <button disabled={locked || !draft.baseline_id}>Save review settings</button>
          </form>
          <div className="drift-review__actions">
            <button disabled={locked || dirty || !state.config?.enabled} onClick={() => execute(() => api.reviewDrift(credential), () => setMessage("Review completed. Results are available below."))}>Run review now</button>
            <button disabled={locked || !state.config?.enabled} onClick={() => execute(() => api.stopDrift(credential), result => { setDraft(d => ({ ...d, enabled: result.enabled })); setMessage("Automatic review paused; evidence retained."); })}>Pause automatic review</button>
          </div>
          {dirty && <p role="status">Unsaved settings. Save before running a review.</p>}
          <p>{state.scheduler}. Running paper sessions are collected automatically. Pause stops future reviews; it does not cancel trading.</p>
        </section>

        <section className="drift-review__section" aria-labelledby="drift-results-title">
          <h2 id="drift-results-title">Comparison results</h2>
          {!report ? <p>No review recorded. Capture a baseline, save settings, then run a review.</p> : <>
            <label>Recorded review<select aria-label="Recorded review" value={reportId} onChange={e => setReportId(e.target.value)}><option value="">Follow latest review</option>{state.reports.map(r => <option value={r.id} key={r.id}>{r.recorded_at} · {r.id}</option>)}</select></label>
            <p role="status">{historical ? "Historical result — captured under earlier settings." : report.id === latest.id ? "Latest recorded review" : "Recorded review"} · {report.recorded_at}</p>
            <code>Model {report.model_version}</code>
            <p>{Object.entries(report.stage_coverage).map(([kind, count]) => `${kind}: ${count ? `${count} comparison source(s)` : "comparison unavailable"}`).join(" · ")}</p>
            {report.source_errors.map(s => <p key={s.source_id} role="alert">{s.source_id}: {s.reason}</p>)}
            {!report.sources.length && <p>No comparison sources available. Import another recorded source with the same contract.</p>}
            {report.sources.map(s => <article className="drift-review__comparison" key={s.source_id}>
              <h3>{s.source_id} · {s.kind}</h3><p>{statusText(s.status)}{s.reason && `: ${s.reason}`}</p>
              {s.groups && Object.entries(s.groups).map(([group, metrics]) => <details key={group} open={group === "All"}>
                <summary>{group === "All" ? "All regimes" : `${group} regime`}</summary>
                <div className="drift-review__table" tabIndex="0" role="region" aria-label={`${s.source_id} ${group} metric comparison`}><table>
                  <caption>Means, sample counts and approximate 95% intervals · {s.as_of}</caption>
                  <thead><tr><th scope="col">Metric</th><th scope="col">Baseline / n / interval</th><th scope="col">Current / n / interval</th><th scope="col">Change / threshold</th><th scope="col">Review state</th></tr></thead>
                  <tbody>{Object.entries(metrics).map(([key, m]) => <tr key={key}><th scope="row">{METRICS[key]?.[0] || key}</th>{[m.baseline, m.current].map((v, i) => <td key={i}>{number(v.mean)}<br />n = {v.n}<br />{v.ci95 ? v.ci95.map(number).join(" to ") : "Interval unavailable"}</td>)}<td>{number(m.delta)} / {number(m.threshold)}</td><td>{statusText(m.status)}</td></tr>)}</tbody>
                </table></div>
              </details>)}
              <details><summary>Source provenance</summary><p>{s.reference}</p><code>SHA-256 {s.sha256}</code></details>
            </article>)}
            <details><summary>Review settings and evidence IDs</summary><p>Settings {report.config_id} · baseline {report.baseline_id || "Unavailable"}</p></details>
          </>}
          <p>{state.limitations}</p>
        </section>
        <section className="drift-review__section" aria-labelledby="drift-alerts-title">
          <h2 id="drift-alerts-title">Alert history</h2>
          <p>Alerts record state transitions, including insufficient evidence, incompatibility, stale sources, threshold breaches and recovery. Review alerts here; no model changes are applied.</p>
          {!state.alerts.length ? <p>No review alerts recorded.</p> : <ol className="drift-review__alerts">{state.alerts.map(a => <li key={a.id}><strong>{statusText(a.status)}</strong> · {a.key}<br />{a.recorded_at}{a.reason && <p>{a.reason}</p>}</li>)}</ol>}
        </section>
      </>}
    </>}
  </section>;
}
