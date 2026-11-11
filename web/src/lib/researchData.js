const metadataFields = ["source", "session", "price_basis", "volume_basis"];
const declarations = (value) => String(value ?? "").split(",").map((part) => part.trim().toLowerCase());

export function missingMetadata(row) {
  return metadataFields.filter((field) => declarations(row[field]).some((value) => !value || value === "unknown"));
}

export function dataGuidance(rows = []) {
  const present = rows.filter((row) => row?.bars > 0);
  const groups = new Map();
  for (const row of present) {
    const fields = missingMetadata(row);
    if (!fields.length) continue;
    const key = fields.join(", ");
    if (!groups.has(key)) groups.set(key, { fields, tickers: [] });
    groups.get(key).tickers.push(row.ticker);
  }
  const midpoint = present.filter((row) => declarations(row.volume_basis).includes("unavailable_midpoint") ||
    declarations(row.source).some((source) => ["ibkr_mid", "mid", "midpoint"].includes(source)));
  const zeroVolume = present.filter((row) => row.zero_volume_bars > 0 && !midpoint.includes(row));
  return { metadata: [...groups.values()], midpoint, zeroVolume };
}

const repairs = [
  ["Cannot read data CSV", "Use Choose Data CSV to select a readable CSV with a header row, then Recheck data."],
  ["Missing CSV columns", "Add the listed missing columns to your CSV, select the corrected file with Choose Data CSV, then Recheck data."],
  ["Select at least one ticker", "Enter at least one ticker in Tickers, then Recheck data."],
  ["No bars", "Import bars for the named ticker with Choose Data CSV, or remove that ticker from Tickers, then Recheck data."],
  ["Invalid or missing timestamps", "Replace missing or invalid timestamps with valid ISO timestamps from the source, then select the corrected CSV and Recheck data. Naive timestamps are interpreted as UTC."],
  ["Duplicate ticker/timestamp rows", "Resolve duplicate ticker/timestamp rows against the source, then select the corrected CSV and Recheck data."],
  ["Rows are out of order", "Sort rows by ticker and timestamp, then select the corrected CSV and Recheck data."],
  ["Missing, non-finite or non-positive OHLC prices", "Repair missing, non-finite or non-positive prices against the source, then select the corrected CSV and Recheck data."],
  ["Invalid OHLC envelope", "Check each affected bar against the source: high must be at least open, close and low; low must be at most open, close and high. Select the corrected CSV and Recheck data."],
  ["Missing or invalid volume", "Import observed volume using Choose Data CSV, then Recheck data. Never fill missing volume with invented values."],
  ["No positive-volume bars", "Use Choose Data CSV to import observed trade-volume bars, then Recheck data. MIDPOINT prices do not provide trade volume."],
  ["MIDPOINT volume is unavailable", "Use Choose Data CSV to import observed trade-volume bars instead of MIDPOINT data, then Recheck data."],
  ["Dataset provenance integrity error", "Re-import the original source CSV or obtain a fresh file from the provider using Choose Data CSV, then Recheck data."],
];

export function dataRepairStep(error) {
  return repairs.find(([message]) => error.includes(message))?.[1] ||
    "Check the named issue against your data supplier, repair or replace the CSV with Choose Data CSV, then Recheck data.";
}
