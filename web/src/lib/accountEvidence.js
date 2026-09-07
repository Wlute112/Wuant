// Recheck the observation timestamp at render time: a successful old HTTP
// response must not keep a broker reading current after polling fails.
export function settledCashLabel(evidence, { connected = false, now = Date.now() } = {}) {
  if (!connected) return "UNKNOWN · disconnected";
  if (!evidence || evidence.currency !== "USD") return "UNAVAILABLE · USD";
  if (evidence.status !== "CURRENT") {
    return `${["STALE", "INVALID", "DISCONNECTED"].includes(evidence.status) ? evidence.status : "UNAVAILABLE"} · USD`;
  }
  const age = now / 1000 - evidence.observed_at;
  if (typeof evidence.observed_at !== "number" || !Number.isFinite(age) || age < 0) return "INVALID · timestamp";
  if (age > 240) return "STALE · USD";
  const value = evidence.value;
  if ((typeof value !== "string" && typeof value !== "number") || String(value).trim() === "" || !Number.isFinite(Number(value))) {
    return "INVALID · amount";
  }
  return `${new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(value))} · USD settled`;
}
