import DataReport from "./DataReport.jsx";

export default function SimulationEvidence({ run }) {
  const evidence = run?.equity_simulation;
  if (!evidence) return <p>This run predates equity simulation evidence.</p>;
  const flows = evidence.cashflows || [];
  const total = flows.reduce((sum, item) => sum + item.amount, 0);
  const metrics = run.metrics || run.oos_metrics || {};
  return <div>
    <dl className="trade-summary">
      <div><dt>Calibration</dt><dd>{evidence.calibration_status}</dd></div>
      <div><dt>Price basis</dt><dd>{evidence.price_basis}</dd></div>
      <div><dt>Bars without liquidity</dt><dd>{evidence.data_coverage?.zero_volume_bars ?? "—"} / {evidence.data_coverage?.bars ?? "—"}</dd></div>
      <div><dt>Commissions & regulatory fees</dt><dd>${evidence.commission_usd?.toFixed(2)}</dd></div>
      <div><dt>Financing & action cashflows</dt><dd>${total.toFixed(2)}</dd></div>
      <div><dt>Excess-return Sharpe</dt><dd>{metrics.sharpe_ratio ?? "—"}</dd></div>
    </dl>
    <p>{evidence.equity_basis}. Sharpe subtracts the configured annual risk-free return over each observed interval.</p>
    <p>{evidence.execution_basis}</p>
    <ul>{evidence.limitations?.map((text) => <li key={text}>{text}</li>)}</ul>
    {evidence.data_preflight && <details className="data-preflight"><summary>Source data preflight at execution</summary><DataReport report={evidence.data_preflight} /></details>}
    <details><summary>Assumptions, cashflows & corporate-action audit</summary><pre className="simulation-evidence">{JSON.stringify(evidence, null, 2)}</pre></details>
  </div>;
}
