import { formatUsd, formatNum } from "../../lib/format.js";
import "../bezel/sector-evidence.css";

export default function SectorRiskEvidence({ evidence, current }) {
  const rows = Object.entries(evidence?.sectors || {});
  return <details className="sector-evidence">
    <summary>Sector exposure · {current ? evidence?.status || "UNKNOWN" : "UNKNOWN · connection or telemetry unavailable"}</summary>
    <p role="status">{current ? evidence?.basis || "Exposure basis unavailable" : "Last reported evidence below is historical. Current exposure and entry permission are unknown."}</p>
    <p>Limit per sector: {Number.isFinite(evidence?.limit_pct) ? `${formatNum(evidence.limit_pct, 2)}% of risk equity` : "UNKNOWN"}. Longs, shorts and outstanding entries count gross; protective exits are excluded.</p>
    {evidence?.issues?.map((issue, index) => <p key={index}>{issue}</p>)}
    {rows.length ? <table className="live-panel__table">
      <caption>{current ? "Reported gross sector exposure" : "Last reported sector exposure · not current"}</caption>
      <thead><tr><th scope="col">Sector</th><th scope="col">Gross USD</th><th scope="col">Equity %</th><th scope="col">Limit status</th></tr></thead>
      <tbody>{rows.map(([name, row]) => <tr key={name}>
        <th scope="row">{name.replaceAll("_", " ")}</th><td>{formatUsd(row.notional)}</td>
        <td>{formatNum(row.equity_pct, 2)}%</td><td>{!current ? "UNKNOWN" : row.breached ? "BREACHED" : "Within limit"}</td>
      </tr>)}</tbody>
    </table> : <p>No valued sector exposure is available.</p>}
    {evidence?.classifications?.map((row) => <p key={row.instrument_id}>
      <strong>{row.symbol}</strong> · conId {row.con_id} · {Object.entries(row.weights || {}).map(([sector, weight]) => `${sector.replaceAll("_", " ")} ${formatNum(weight * 100, 2)}%`).join(", ")}<br />
      Source: {row.source_name} · <a href={row.source_url?.startsWith("https://") ? row.source_url : undefined} target="_blank" rel="noreferrer">Source document</a><br />
      As of {row.as_of} · reviewed by {row.reviewed_by} at {row.reviewed_at} · expires {row.valid_until}
    </p>)}
    {evidence?.evidence_sha256 && <p>Evidence SHA-256: {evidence.evidence_sha256}</p>}
    <p>Missing, expired or mismatched evidence blocks entries. Stop the session, replace the reviewed evidence in configuration, and restart to reconcile. A disconnect also requires fresh reconciliation.</p>
  </details>;
}
