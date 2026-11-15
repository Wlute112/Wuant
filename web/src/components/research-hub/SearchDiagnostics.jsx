export default function SearchDiagnostics({ diagnostics }) {
  if (!diagnostics) return <p>Detailed rejection evidence is unavailable for this run. Review its trial history and logs.</p>;
  const rejected = diagnostics.rejected_trials || [];
  const fraction = diagnostics.min_positive_fold_fraction;
  return <div className="search-diagnostics">
    <p>{diagnostics.completed_trials} completed trials · {diagnostics.qualifying_trials} qualifying · {diagnostics.pruned_trials} pruned · {diagnostics.failed_trials} failed.</p>
    {fraction != null && <p>Qualification requires positive net risk-adjusted returns under normal costs in at least {Number((fraction * 100).toFixed(2))}% of validation folds.</p>}
    {diagnostics.require_active_folds && <p>Every validation fold must execute trades under both normal and stressed costs. Inactive candidates are rejected before the final holdout.</p>}
    {!!rejected.length && <>
      <ul>{rejected.slice(0, 3).map(trial => <li key={trial.trial_number}>Trial {trial.trial_number}: {trial.reasons.join(" ")}</li>)}</ul>
      <details>
        <summary>Rejection details · {rejected.length} trials</summary>
        <div className="research-table-wrap" tabIndex={0} role="region" aria-label="Rejected trial details">
          <table className="research-table">
            <thead><tr><th scope="col">Trial</th><th scope="col">Positive folds</th><th scope="col">Required</th><th scope="col">Filled orders</th><th scope="col">Inactive folds</th><th scope="col">Reason</th></tr></thead>
            <tbody>{rejected.map(trial => <tr key={trial.trial_number}>
              <th scope="row">{trial.trial_number}</th>
              <td className="num">{trial.positive_folds == null ? "Unavailable" : `${trial.positive_folds} / ${trial.fold_count}`}</td>
              <td className="num">{trial.required_positive_folds ?? "Unavailable"}</td>
              <td className="num">{trial.trades ?? "Unavailable"}</td>
              <td className="num">{trial.inactive_folds ?? "Unavailable"}</td>
              <td className="search-diagnostics__reasons"><ul>{trial.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul></td>
            </tr>)}</tbody>
          </table>
        </div>
      </details>
    </>}
    <p>Additional development trials may find a qualifier, but a qualifying result is not guaranteed.</p>
  </div>;
}
