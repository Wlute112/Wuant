export function killSwitchStatus(risk, { statusUnknown = false, isDemo = false } = {}) {
  if (isDemo) return "DEMO";
  if (statusUnknown || typeof risk?.kill_switch_engaged !== "boolean") return "UNKNOWN";
  return risk.kill_switch_engaged ? "ENGAGED" : "CLEAR";
}
