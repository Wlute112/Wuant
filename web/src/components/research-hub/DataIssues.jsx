import { dataGuidance, dataRepairStep } from "../../lib/researchData.js";

export function DataErrors({ errors = [] }) {
  if (!errors.length) return null;
  return <div className="data-checks__errors data-checks__issues" role="alert">
    {Array.from(new Set(errors)).map((error) => <div key={error}>
      <p>{error}</p>
      <p className="data-checks__next-step"><strong>Next step:</strong> {dataRepairStep(error)}</p>
    </div>)}
  </div>;
}

export function DataGuidance({ rows, eligible }) {
  const { metadata, midpoint, zeroVolume } = dataGuidance(rows);
  if (!metadata.length && !midpoint.length && !zeroVolume.length) return null;
  return <div className="data-checks__issues data-checks__guidance" aria-label="Data limitations and next steps">
    <p><strong>{eligible ? "Non-blocking data limitations" : "Additional data limitations"}</strong></p>
    {midpoint.length > 0 && <div>
      <p>{midpoint.map((row) => row.ticker).join(", ")}: MIDPOINT bars provide prices without observed trade volume.</p>
      <p><strong>Next step:</strong> Use Choose Data CSV to import observed trade volume with documented price and volume basis, then Recheck data.</p>
    </div>}
    {zeroVolume.length > 0 && <div>
      <p>{zeroVolume.map((row) => `${row.ticker} (${row.zero_volume_bars.toLocaleString()} / ${row.bars.toLocaleString()} bars)`).join(", ")}: zero volume reported.</p>
      <p><strong>Next step:</strong> Verify these bars with your supplier. If volume is missing or incorrect, use Choose Data CSV to import corrected observed volume, then Recheck data.</p>
    </div>}
    {metadata.length > 0 && <div>
      {metadata.map(({ fields, tickers }) => <p key={fields.join(",")}>
        {tickers.join(", ")}: undeclared {fields.join(", ")}.
      </p>)}
      <p><strong>Next step:</strong> Verify the missing declarations in the supplier documentation. Add only documented values to the CSV, select it with Choose Data CSV, then Recheck data.</p>
    </div>}
  </div>;
}
