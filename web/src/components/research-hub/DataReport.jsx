import "./data-preflight.css";

export default function DataReport({ report }) {
  if (!report) return null;
  return <>
    <p>Diagnostics only. Zero-volume bars supply no equity execution liquidity.</p>
    {!!report.errors?.length && <ul>{report.errors.map((text) => <li key={text}>{text}</li>)}</ul>}
    <div className="data-preflight__table" tabIndex={0} role="region" aria-label="Per-ticker data coverage">
      <table>
        <caption>Observed coverage and declared source semantics</caption>
        <thead><tr><th scope="col">Ticker / bars</th><th scope="col">UTC coverage / cadence</th><th scope="col">Volume</th><th scope="col">Source / session / basis</th></tr></thead>
        <tbody>{report.tickers?.map((row) => <tr key={row.ticker}>
          <th scope="row">{row.ticker}<br />{row.bars} bars</th>
          <td>{row.start || "Unavailable"}<br />{row.end || "Unavailable"}<br />{row.cadence_minutes == null ? "Cadence unavailable" : `${row.cadence_minutes} min observed`}{row.requested_bar_hours != null && <><br />{row.requested_bar_hours}h requested</>}</td>
          <td>{row.zero_volume_bars} zero / {row.bars}<br />{row.missing_volume_bars} missing<br />{row.invalid_volume_bars} invalid</td>
          <td>{row.source}<br />{row.session}<br />{row.price_basis}<br />{row.volume_basis}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {!!report.warnings?.length && <details><summary>Coverage limitations ({report.warnings.length})</summary><ul>{report.warnings.map((text) => <li key={text}>{text}</li>)}</ul></details>}
    <p>{report.coverage_note}</p>
    {report.sha256 && <details><summary>Dataset fingerprint</summary><code className="data-preflight__hash">SHA-256 {report.sha256}</code></details>}
  </>;
}
