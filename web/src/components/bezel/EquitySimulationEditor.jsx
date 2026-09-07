/* Extends the research configuration form using its existing field grammar.
 * Users choose explicit execution/cost assumptions and attach dated evidence;
 * defaults remain visibly uncalibrated. No new visual identity or navigation.
 */
import { useState } from "react";

export const DEFAULT_SIMULATION = {
  price_basis: "split_adjusted", path: "open_low_high_close",
  source: "Uncalibrated conservative scenario; regulatory fees require dated inputs",
  spread_bps: 5, impact_bps: 5, participation: 0.01,
  extended_liquidity: 0.25, extended_spread_multiplier: 2,
  commission_per_share: 0.005, minimum_commission: 1, commission_cap_pct: 0.01,
  sec_sell_rate: 0, taf_per_share: 0, taf_cap: 0, cat_per_share: 0,
  borrow_rate: 0.03, debit_rate: 0.06, cash_credit_rate: 0, risk_free_rate: 0,
  calibrations: [], corporate_actions: [],
};

const FIELDS = [
  ["spread_bps", "Full spread (bps)", 0, 1000], ["impact_bps", "Second-level impact (bps)", 0, 1000],
  ["participation", "Maximum bar participation (fraction)", 0.000001, 0.25],
  ["extended_liquidity", "Extended-hours liquidity fraction", 0, 1],
  ["extended_spread_multiplier", "Extended-hours spread multiplier", 1, 20],
  ["commission_per_share", "Commission / share (USD)", 0, 1],
  ["minimum_commission", "Minimum / order (USD)", 0, 100],
  ["commission_cap_pct", "Order commission cap (notional fraction)", 0.000001, 1],
  ["sec_sell_rate", "SEC sell-notional fee (fraction)", 0, 0.01],
  ["taf_per_share", "FINRA TAF / sold share (USD)", 0, 1],
  ["taf_cap", "FINRA TAF cap / order (USD)", 0, 100],
  ["cat_per_share", "CAT / share (USD)", 0, 1],
  ["borrow_rate", "Annual stock borrow rate (fraction)", 0, 10],
  ["debit_rate", "Annual debit interest (fraction)", 0, 1],
  ["cash_credit_rate", "Annual effective cash credit (fraction)", 0, 1],
  ["risk_free_rate", "Annual risk-free return (fraction)", 0, 1],
];

export default function EquitySimulationEditor({ value, onChange }) {
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const cfg = { ...DEFAULT_SIMULATION, ...value };
  const set = (key, next) => onChange({ ...cfg, [key]: next });
  async function load(event, key) {
    const file = event.target.files?.[0];
    if (!file) return;
    setLoading(true); setError("");
    try {
      if (file.size > 2_000_000) throw new Error("Use an evidence file smaller than 2 MB.");
      const data = JSON.parse(await file.text());
      if (!Array.isArray(data)) throw new Error("The evidence file must contain a JSON array.");
      if (data.length > 10000) throw new Error("Use at most 10,000 evidence records.");
      set(key, data);
    } catch (err) { setError(err.message); }
    finally { setLoading(false); event.target.value = ""; }
  }
  return <details className="simulation-editor">
    <summary>Equity execution & costs</summary>
    <p>Finite liquidity, partial fills and opening gaps. Assumptions are fixed across the sweep; stress scales spread, impact, fees and financing costs.</p>
    <p>{cfg.calibrations.length ? `${cfg.calibrations.length} dated calibrations attached.` : "Uncalibrated scenario. Regulatory fees default to zero; supply dated rates for historical cost estimates."}</p>
    <div className="action-panel__ibkr">
      <label className="action-panel__field">Price basis<select value={cfg.price_basis} onChange={(e) => set("price_basis", e.target.value)}><option value="split_adjusted">Split-adjusted, dividends excluded</option><option value="raw">Raw prices + explicit split events</option></select></label>
      <label className="action-panel__field">Intrabar scenario<select value={cfg.path} onChange={(e) => set("path", e.target.value)}><option value="open_low_high_close">Open → low → high → close</option><option value="open_high_low_close">Open → high → low → close</option></select></label>
      {FIELDS.map(([key, label, min, max]) => <label className="action-panel__field" key={key}>{label}<input type="number" min={min} max={max} step="any" required value={cfg[key]} onChange={(e) => set(key, e.target.value === "" ? "" : Number(e.target.value))} /></label>)}
    </div>
    <label className="action-panel__field">Assumption source<input required minLength={5} value={cfg.source} onChange={(e) => set("source", e.target.value)} /></label>
    <p>Rates use fractions: 0.03 = 3% annually. Compare both intrabar scenarios because OHLC bars cannot establish which extreme occurred first. Zero-volume bars offer no liquidity.</p>
    {[["calibrations", "Dated calibration JSON"], ["corporate_actions", "Corporate-action JSON"]].map(([key, label]) => <div key={key}>
      <label className="action-panel__field">{label}<input type="file" accept=".json,application/json" disabled={loading} onChange={(e) => load(e, key)} /></label>
      <p>{cfg[key].length} records attached {cfg[key].length > 0 && <button type="button" onClick={() => set(key, [])}>Remove {label.toLowerCase()}</button>}</p>
      {cfg[key].length > 0 && <details><summary>Review attached records</summary><pre className="simulation-evidence">{JSON.stringify(cfg[key], null, 2)}</pre></details>}
    </div>)}
    <details><summary>Evidence file formats</summary>
      <p>Calibrations are full snapshots of the numeric fields above plus ticker (or *), source, observed_through and effective_at. Omitted numeric fields use the documented scenario defaults. Evidence must precede its effective date.</p>
      <pre className="simulation-evidence">{JSON.stringify([{ ticker: "SPY", observed_through: "2025-12-31T21:00:00Z", effective_at: "2026-01-02T14:30:00Z", source: "Your measured execution sample", spread_bps: 2, impact_bps: 3 }], null, 2)}</pre>
      <p>Actions require event_id, ticker (the stable research symbol), kind, effective_at and source. SPLIT adds split_ratio; CASH_DIVIDEND adds cash_per_share and pay_at; SYMBOL_CHANGE adds new_symbol. Explicit splits require raw prices.</p>
      <pre className="simulation-evidence">{JSON.stringify([{ event_id: "example-split", ticker: "ABC", kind: "SPLIT", effective_at: "2026-01-02T00:00:00Z", source: "Your issuer notice", split_ratio: 2 }], null, 2)}</pre>
    </details>
    {loading && <p role="status">Reading evidence…</p>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
