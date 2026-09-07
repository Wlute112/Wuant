export const DEFAULT_SESSION_POLICY = {
  mode: "AUTO",
  windows: "09:30-16:00",
  opening_buffer_minutes: 5,
  closing_buffer_minutes: 5,
  no_new_entry_minutes_before_close: 15,
  participate_opening_auction: false,
  participate_closing_auction: false,
  cancel_entries_at_session_end: true,
  overnight_pnl_assignment: "NEXT_SESSION",
};

export function sessionPolicyPayload(value, includeExtendedHours) {
  const { windows, mode, ...settings } = value;
  const resolvedMode = mode === "AUTO"
    ? (includeExtendedHours ? "EXTENDED_HOURS" : "RTH_ONLY") : mode;
  if (resolvedMode !== "RTH_ONLY" && !includeExtendedHours) {
    throw new Error("Enable Include extended hours to use an extended or custom session.");
  }
  const customWindows = resolvedMode === "CUSTOM"
    ? windows.split(",").map((window) => window.trim().split("-")) : [];
  if (customWindows.some((pair) => pair.length !== 2 || pair[0] === pair[1]
      || pair.some((time) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)))) {
    throw new Error("Use custom windows such as 09:30-12:00,13:00-16:00 with different start and end times.");
  }
  for (const key of ["opening_buffer_minutes", "closing_buffer_minutes", "no_new_entry_minutes_before_close"]) {
    if (settings[key] === "" || !Number.isInteger(Number(settings[key]))
        || Number(settings[key]) < 0 || Number(settings[key]) > 1440) {
      throw new Error("Session buffers must be whole minutes between 0 and 1440.");
    }
    settings[key] = Number(settings[key]);
  }
  return { ...settings, mode: resolvedMode, custom_windows: customWindows };
}
