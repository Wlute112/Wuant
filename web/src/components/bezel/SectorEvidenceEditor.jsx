import { useEffect, useId, useRef, useState } from "react";
import { api } from "../../lib/api.js";
import { sectorEvidenceStatus } from "../../lib/sectorRisk.js";
import "./sector-evidence.css";

export default function SectorEvidenceEditor({ value, onChange, tickers }) {
  const id = useId();
  const input = useRef(null);
  const generation = useRef(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => () => { generation.current++; }, []);
  async function upload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    const current = ++generation.current;
    setBusy(true); setError("");
    try {
      if (file.size > 1024 * 1024) throw new Error("Sector evidence must be at most 1 MB.");
      const evidence = JSON.parse(await file.text());
      const result = await api.validateSectorEvidence(evidence, tickers);
      if (generation.current === current) onChange(result.evidence);
    } catch (err) {
      if (generation.current === current) {
        onChange(null);
        setError(`Evidence was not accepted: ${err.message}`);
      }
    } finally {
      if (generation.current === current) setBusy(false);
      if (input.current) input.current.value = "";
    }
  }
  return <section className="sector-evidence" aria-labelledby={`${id}-title`}>
    <h3 id={`${id}-title`} className="label">Sector evidence for execution</h3>
    <p>Upload reviewed issuer or classification-provider weights for every selected symbol, including ETF holdings. Evidence must match the broker contract ID, cover 100% of exposure, and expire within 31 days of its source date. Research sector maps do not authorize entries.</p>
    <label htmlFor={`${id}-file`}>Reviewed sector evidence (JSON)</label>
    <input id={`${id}-file`} ref={input} type="file" accept=".json,application/json" onChange={upload} disabled={busy} />
    <p role="status">{busy ? "Validating evidence…" : sectorEvidenceStatus(value, tickers)}</p>
    {error && <p role="alert">{error}</p>}
    {value && <>
      <ul>{(Array.isArray(value.classifications) ? value.classifications : []).filter((row) => row && typeof row === "object").map((row) => <li key={row.con_id}>
        {row.symbol} · conId {row.con_id} · {row.source_name} · expires {row.valid_until}
      </li>)}</ul>
      <button type="button" onClick={() => { generation.current++; setBusy(false); onChange(null); setError(""); }}>Remove evidence</button>
    </>}
    <details><summary>Required JSON format</summary>
      <p>Replace every placeholder with reviewed source evidence. Weights are fractions. Accepted sectors: communication_services, consumer_discretionary, consumer_staples, energy, financials, healthcare, industrials, materials, real_estate, technology, utilities. Partial holdings and unsupported asset exposures cannot be treated as fully classified.</p>
      <pre>{JSON.stringify({ schema_version: 1, classifications: [{ con_id: "IBKR integer conId", symbol: "SYMBOL", weights: { technology: 1 }, source_name: "Issuer or classification provider", source_url: "https://source.example/document", as_of: "Source timestamp with timezone", reviewed_at: "Review timestamp with timezone", reviewed_by: "Reviewer name", valid_until: "Expiry timestamp with timezone" }] }, null, 2)}</pre>
    </details>
    <p>Without current matching evidence, a session may warm up and report status, but entries remain blocked. To replace evidence during a session, stop it, load the replacement, and restart for reconciliation.</p>
  </section>;
}
