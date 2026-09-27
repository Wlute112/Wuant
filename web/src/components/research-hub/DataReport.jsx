import "./data-preflight.css";
import DatasetProvenance from "./DatasetProvenance.jsx";

export default function DataReport({ report }) {
  if (!report) return null;
  const synthetic = report.tickers?.some((row) => row.source?.split(", ").includes("synthetic_fixture"));
  const fixtureCalendar = report.tickers?.find((row) => row.fixture_calendar)?.fixture_calendar;
  const shortFixture = report.tickers?.some((row) => row.source?.split(", ").includes("synthetic_fixture") && row.bars <= 150);
  return <>
    {synthetic && <p role="note"><strong>Synthetic fixture.</strong> Prices and volume are generated for pipeline testing; they are not observed market evidence.
      {fixtureCalendar && <><br />{fixtureCalendar.calendar} v{fixtureCalendar.version || "unknown"} · {fixtureCalendar.sessions} verified sessions · {fixtureCalendar.early_close_sessions} early closes · UTC opens {fixtureCalendar.utc_open_times?.join(", ") || "unavailable"}</>}
      {shortFixture && <><br />Short history: the default strategy needs 150 warmup bars and additional bars for trades or evaluation.</>}
    </p>}
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
    <DatasetProvenance evidence={report.provenance} />
    {report.sha256 && <details><summary>Dataset fingerprint</summary><code className="data-preflight__hash">SHA-256 {report.sha256}</code></details>}
  </>;
}
