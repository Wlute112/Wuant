import "./data-preflight.css";

function Changes({ changes }) {
  if (!changes) return <p>No earlier locally observed version is available for comparison.</p>;
  return <p>{changes.added_bars ?? "Unknown"} added bars · {changes.removed_bars ?? "Unknown"} removed · {changes.revised_bars ?? "Unknown"} revised (excluding retrieval-time changes).<br />
    Added symbols: {changes.added_symbols?.join(", ") || "None"}. Removed symbols: {changes.removed_symbols?.join(", ") || "None"}.</p>;
}

export default function DatasetProvenance({ evidence }) {
  if (!evidence) return <p>Dataset provenance unavailable for this record. Historical universe membership and survivorship coverage are unknown.</p>;
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(evidence, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `dataset-${evidence.sha256 || "unknown"}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <details className="data-provenance">
    <summary>Dataset provenance & universe · {evidence.status === "archived" ? "Archived snapshot" : evidence.status === "integrity_error" ? "Integrity error" : "Not yet archived"}</summary>
    {evidence.error && <p role="alert">{evidence.error}. Restore the retained data and manifest before research.</p>}
    <p>{evidence.observed_at ? `First observed locally: ${evidence.observed_at}` : "No local archive observation yet. Launching research retains the exact input bytes."}</p>
    <code className="data-preflight__hash">SHA-256 {evidence.sha256}</code>
    {evidence.manifest_sha256 && <p className="data-preflight__hash">Manifest SHA-256 {evidence.manifest_sha256}</p>}
    <Changes changes={evidence.changes} />
    <div className="data-preflight__table" tabIndex={0} role="region" aria-label="Dataset universe and lineage">
      <table>
        <caption>All supplied symbols; dates below are observed bar coverage, not membership dates</caption>
        <thead><tr><th scope="col">Symbol / coverage</th><th scope="col">Supplier declarations</th></tr></thead>
        <tbody>{evidence.universe?.map((row) => <tr key={row.ticker}>
          <th scope="row">{row.ticker} · {row.bars} bars<br />{row.observed_start || "Unknown"}<br />{row.observed_end || "Unknown"}</th>
          <td>{row.declarations?.map((item, index) => <details key={index}>
            <summary>{item.source || "Unknown source"} · retrieved {item.retrieved_at || "unknown"}</summary>
            <dl>{[
              ["Session", item.session], ["Price basis", item.price_basis], ["Volume basis", item.volume_basis],
              ["Broker conId", item.con_id], ["Symbol alias", item.symbol_alias],
              ["Membership start", item.membership_start], ["Membership end", item.membership_end],
              ["Membership evidence source", item.membership_source],
            ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || "Unknown / undeclared"}</dd></div>)}</dl>
          </details>)}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {!!evidence.history?.length && <details><summary>Earlier dataset observations ({evidence.history.length}{evidence.history_truncated ? "+" : ""})</summary>
      <ol>{evidence.history.map((item) => <li key={`${item.sha256}-${item.observed_at}`}><p>{item.observed_at} · {item.operation}</p><code className="data-preflight__hash">{item.sha256}</code><Changes changes={item.changes} /></li>)}</ol>
      {evidence.history_truncated && <p>Showing the most recent 50 ancestors. Older versions remain in the local archive.</p>}
    </details>}
    <ul>{evidence.limitations?.map((text) => <li key={text}>{text}</li>)}</ul>
    <button type="button" onClick={download}>Download provenance evidence</button>
  </details>;
}
