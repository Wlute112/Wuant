import { missingMetadata } from "../../lib/researchData.js";
import { DataErrors, DataGuidance } from "./DataIssues.jsx";

const dateFormat = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "short", day: "numeric", timeZone: "UTC",
});

function Check({ state, children }) {
  return <li className={`data-checks__item is-${state}`}>
    <span className="data-checks__mark" aria-hidden="true">{state === "passed" ? "✓" : state === "unknown" ? "—" : "!"}</span>
    <span>{children}</span>
  </li>;
}

export default function DataCheckSummary({ report, request }) {
  if (!report) return null;
  const reported = report.tickers || [];
  const rows = request.tickers?.length
    ? request.tickers.map((ticker) => reported.find((row) => row.ticker === ticker))
    : reported;
  const covered = rows.filter((row) => row?.bars > 0).length;
  const allCovered = rows.length > 0 && covered === rows.length;
  const volumeKnown = allCovered && rows.every((row) => row &&
    [row.bars, row.zero_volume_bars, row.missing_volume_bars, row.invalid_volume_bars].every(Number.isFinite));
  const volumeValid = volumeKnown && rows.every((row) => row.missing_volume_bars === 0 && row.invalid_volume_bars === 0 &&
    (request.asset_class !== "equity" || row.zero_volume_bars < row.bars));
  const zeroVolume = reported.reduce((sum, row) => sum + (row.zero_volume_bars || 0), 0);
  const declared = allCovered && rows.every((row) => missingMetadata(row).length === 0);
  const starts = reported.map((row) => Date.parse(row.start)).filter(Number.isFinite);
  const ends = reported.map((row) => Date.parse(row.end)).filter(Number.isFinite);
  const bars = reported.reduce((sum, row) => sum + (row.bars || 0), 0);
  const synthetic = reported.some((row) => row.source?.split(", ").includes("synthetic_fixture"));
  const shortFixture = reported.some((row) => row.source?.split(", ").includes("synthetic_fixture") && row.bars <= 150);
  const errors = report.errors || [];

  return <div className="data-checks">
    <ul className="data-checks__list" aria-label="Data check results">
      <Check state={allCovered ? "passed" : "warning"}>{covered} / {rows.length} tickers covered</Check>
      <Check state={report.execution_eligible && allCovered ? "passed" : "warning"}>
        {report.execution_eligible && allCovered ? "Prices & timestamps checked" : "Prices & timestamps need review"}
      </Check>
      <Check state={!volumeKnown ? "unknown" : !volumeValid ? "failed" : zeroVolume ? "warning" : "passed"}>
        {!volumeKnown ? "Volume checks unavailable" : !volumeValid ? "Volume needs repair" : zeroVolume ? `${zeroVolume.toLocaleString()} zero-volume bars` : "Volume checked"}
      </Check>
      <Check state={declared ? "passed" : "warning"}>{declared ? "Source & session declared" : "Metadata incomplete"}</Check>
    </ul>
    {bars > 0 && <p className="data-checks__coverage">{bars.toLocaleString()} bars
      {starts.length > 0 && ends.length > 0 && <> · {dateFormat.format(Math.min(...starts))} – {dateFormat.format(Math.max(...ends))}</>}
    </p>}
    {synthetic && <p className="data-checks__warning" role="note">Synthetic data. For pipeline testing only; prices and volume are generated.
      {shortFixture && <> Short history: the default model needs 150 warmup bars before evaluation.</>}
    </p>}
    <DataErrors errors={errors} />
    <DataGuidance rows={rows} eligible={report.execution_eligible} />
  </div>;
}
