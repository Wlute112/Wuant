export default function SessionPolicyEditor({ value, onChange, Field }) {
  const set = (key, next) => onChange({ ...value, [key]: next });
  return (
    <details className="action-panel__trading-advanced">
      <summary className="label">Session policy</summary>
      <p>Windows use the exchange timezone and are clipped to IBKR trading hours, including holidays and early closes. Custom windows require Include extended hours. Buffers still apply when auction participation is enabled.</p>
      <div className="action-panel__fields">
        <Field label="Entry session">
          <select value={value.mode} onChange={(e) => set("mode", e.target.value)}>
            <option value="AUTO">Match regular / extended hours</option>
            <option value="RTH_ONLY">Regular hours only</option>
            <option value="EXTENDED_HOURS">Extended hours</option>
            <option value="CUSTOM">Custom windows</option>
          </select>
        </Field>
        {value.mode === "CUSTOM" && <Field label="Custom windows (HH:MM-HH:MM)">
          <input value={value.windows} onChange={(e) => set("windows", e.target.value)} placeholder="09:30-12:00,13:00-16:00" />
        </Field>}
        {[
          ["opening_buffer_minutes", "Opening buffer (minutes)"],
          ["closing_buffer_minutes", "Closing buffer (minutes)"],
          ["no_new_entry_minutes_before_close", "Stop new entries before close (minutes)"],
        ].map(([key, label]) => <Field label={label} key={key}>
          <input type="number" min="0" max="1440" step="1" value={value[key]} onChange={(e) => set(key, e.target.value)} />
        </Field>)}
        {[
          ["participate_opening_auction", "Allow entries during opening auction"],
          ["participate_closing_auction", "Allow entries during closing auction"],
          ["cancel_entries_at_session_end", "Cancel resting entries at window end"],
        ].map(([key, label]) => <Field label={label} key={key}>
          <input type="checkbox" checked={value[key]} onChange={(e) => set(key, e.target.checked)} />
        </Field>)}
        <Field label="PnL between exchange sessions" wide>
          <select value={value.overnight_pnl_assignment} onChange={(e) => set("overnight_pnl_assignment", e.target.value)}>
            <option value="NEXT_SESSION">Assign to next trading session</option>
            <option value="PRIOR_SESSION">Assign to prior trading session</option>
          </select>
        </Field>
      </div>
    </details>
  );
}
