import "./risk-panel.css";

export const DEFAULT_RISK = {
  risk_budget_pct: 1,
  max_trade_risk_pct: 0.25,
  max_leverage: 1,
  daily_loss_limit_pct: 2,
  kill_switch_pct: 10,
  kill_warn_pct: 5,
  kelly_max_fraction: 50,
};

const FIELDS = [
  { key: "risk_budget_pct", label: "Risk budget / trade (%)" },
  { key: "max_trade_risk_pct", label: "Hard cap / trade (%)" },
  { key: "max_leverage", label: "Max leverage (x)" },
  { key: "daily_loss_limit_pct", label: "Daily loss limit (%)" },
  { key: "kill_warn_pct", label: "Drawdown warn (%)" },
  { key: "kill_switch_pct", label: "Kill-switch (%)" },
  { key: "kelly_max_fraction", label: "Kelly ceiling (%)" },
];

/** Controlled risk-rail editor. Values are shown/edited as PERCENTAGES for
 * readability; converted to the fractions RiskConfig actually stores
 * (see strategies/risk.py) only at submit time via `toRiskOverrides`. */
export default function RiskPanel({ value, onChange }) {
  return (
    <section className="risk-panel" aria-labelledby="risk-rails-title">
      <h3 id="risk-rails-title" className="label risk-panel__heading">
        Risk Rails (editable per run)
      </h3>
      <div className="risk-panel__grid">
        {FIELDS.map((f) => (
          <label className="risk-panel__field" key={f.key}>
            <span className="label">{f.label}</span>
            <input
              type="number"
              step={f.key === "max_leverage" ? 0.1 : 0.01}
              value={value[f.key]}
              onChange={(e) => onChange({ ...value, [f.key]: Number(e.target.value) })}
            />
          </label>
        ))}
      </div>
    </section>
  );
}

/** RiskPanel edits leverage as a bare number and everything else as a
 * percentage; RiskConfigOverrides (api/schemas.py) expects leverage as-is
 * and every *_pct field as a fraction (0.01 = 1%). */
export function toRiskOverrides(risk) {
  return {
    risk_budget_pct: risk.risk_budget_pct / 100,
    max_trade_risk_pct: risk.max_trade_risk_pct / 100,
    max_leverage: risk.max_leverage,
    daily_loss_limit_pct: risk.daily_loss_limit_pct / 100,
    kill_switch_pct: risk.kill_switch_pct / 100,
    kill_warn_pct: risk.kill_warn_pct / 100,
    kelly_max_fraction: risk.kelly_max_fraction / 100,
  };
}
