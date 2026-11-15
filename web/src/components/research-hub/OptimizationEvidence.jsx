import { useEffect, useState } from "react";
import { api } from "../../lib/api.js";
import { formatNum, formatUsd } from "../../lib/format.js";
import ChannelStrip from "../channel-strip/ChannelStrip.jsx";
import "./optimization-evidence.css";

const score = value => value == null ? "Pending" : value <= -1000000 ? "Not qualifying" : formatNum(value, 4);
const date = value => value ? value.slice(0, 10) : "Unavailable";

export default function OptimizationEvidence({ job, updatedAt }) {
  const [trials, setTrials] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [foldIndex, setFoldIndex] = useState(0);
  const [cost, setCost] = useState("normal");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let disposed = false;
    api.getJobEvidence(job.id).then(data => {
      if (disposed) return;
      setTrials(data.trials || []);
      setError(null);
    }).catch(cause => { if (!disposed) setError(cause.message); });
    return () => { disposed = true; };
  }, [job.id, updatedAt, job.status]);
  const trial = selected ?? trials.at(-1)?.trial;
  useEffect(() => {
    let disposed = false;
    setDetail(null);
    if (trial == null) return;
    setLoading(true);
    api.getJobEvidence(job.id, trial).then(data => {
      if (!disposed) { setDetail(data); setError(null); }
    }).catch(cause => { if (!disposed) setError(cause.message); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [job.id, trial, updatedAt, job.status]);
  const fold = detail?.folds?.[Math.min(foldIndex, (detail?.folds?.length || 1) - 1)];
  const result = fold?.[cost];
  const evidence = result?.detail;
  const points = evidence?.equity || [];
  const base = points[0]?.equity;
  const series = points.map(p => ({ x: Date.parse(p.ts), y: base ? p.equity / base * 100 : 100 }));
  const leader = [...trials].reverse().find(t => t.state === "complete" && t.score > -1000000 && t.score === Math.max(...trials.filter(t => t.state === "complete").map(t => t.score ?? -Infinity)));
  const fills = evidence?.fills?.rows || [];
  const begin = Date.parse(fold?.validation_start);
  const end = Date.parse(fold?.validation_end);
  const fillTime = fill => fill.evidence_time_ns ? Number(fill.evidence_time_ns) / 1e6 : Date.parse(fill.ts_last || fill.ts_event);
  const latestTime = Date.parse(updatedAt);
  return <section className="optimization-evidence" aria-label="Optimization evidence">
    <h2>Inside the search</h2>
    <p>Each trial tests a parameter set across historical validation folds. Evidence appears after a fold completes; workers may compute other folds in parallel. Development leadership does not imply holdout approval.</p>
    {Number.isFinite(latestTime) && <p>Last evidence snapshot: {new Date(latestTime).toLocaleTimeString()}{Date.now() - latestTime > 30000 && ["running", "starting"].includes(job.status) ? " · No recent update; a fold may still be computing. Review logs for activity." : ""}</p>}
    {error && <p role="alert">Evidence unavailable: {error}. The run can continue; progress and logs remain above.</p>}
    {!trials.length && <p>No fold evidence captured yet. Older runs retain their original summaries and logs.</p>}
    {!!trials.length && <>
      <div className="optimization-evidence__controls">
        <label>Inspect trial <select value={trial ?? ""} onChange={event => { setSelected(Number(event.target.value)); setFoldIndex(0); }}>
          {trials.map(t => <option key={t.trial} value={t.trial}>Trial {t.trial} · {t.state} · {score(t.score)}</option>)}
        </select></label>
        {selected != null && <button type="button" onClick={() => { setSelected(null); setFoldIndex(0); }}>Follow latest trial</button>}
        <span>Best qualifying captured trial: {leader ? `${leader.trial} (${score(leader.score)})` : "none yet"}</span>
      </div>
      <details><summary>Trial decision history · {trials.length} captured trials</summary><div className="optimization-evidence__scroll"><table><thead><tr><th>Trial</th><th>Outcome</th><th>Score</th><th>Completed folds</th></tr></thead><tbody>{trials.map(t => <tr key={t.trial}><td><button type="button" onClick={() => { setSelected(t.trial); setFoldIndex(0); }}>Inspect trial {t.trial}</button></td><td>{t.state}{leader?.trial === t.trial ? " · best qualifying captured candidate" : ""}<br />{t.reason}</td><td>{t.score == null ? "Pending" : score(t.score)}</td><td>{t.folds_complete}</td></tr>)}</tbody></table></div></details>
      {loading && <p role="status">Loading captured evidence…</p>}
      {detail && <>
        <p role="status">Trial {detail.trial} · {detail.state}. {detail.reason || "Evaluation in progress. Scores remain provisional until all folds complete."}</p>
        {detail.development_assessment?.passed === false && <div className="optimization-evidence__rejections">
          <h3>Why this trial was rejected</h3>
          <ul>{detail.development_assessment.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
        </div>}
        <details><summary>Parameters and changes from the development leader</summary>
          <div className="optimization-evidence__scroll"><table><thead><tr><th>Parameter</th><th>This trial</th><th>Leader</th></tr></thead><tbody>
            {Object.entries(detail.params || {}).map(([key, value]) => <tr key={key}><th>{key}</th><td>{String(value)}</td><td>{String(leader?.params?.[key] ?? "Unavailable")}</td></tr>)}
          </tbody></table></div>
          <details><summary>Effective settings, including fixed controls</summary><pre>{JSON.stringify(detail.effective_params, null, 2)}</pre></details>
        </details>
        <div className="optimization-evidence__controls">
          <label>Completed validation fold <select value={fold ? Math.min(foldIndex, detail.folds.length - 1) : ""} onChange={event => setFoldIndex(Number(event.target.value))} disabled={!detail.folds?.length}>
            {!detail.folds?.length && <option value="">Waiting for first fold</option>}
            {(detail.folds || []).map((f, index) => <option key={f.fold} value={index}>Fold {f.fold} · {date(f.validation_start)} to {date(f.validation_end)}</option>)}
          </select></label>
          <label>Cost assumption <select value={cost} onChange={event => setCost(event.target.value)}><option value="normal">Normal costs</option><option value="stressed">Stressed costs</option></select></label>
        </div>
        {fold && <>
          <ol className="optimization-evidence__timeline" aria-label="Validation boundaries">
            <li><strong>Train</strong><span>{date(fold.training_start)} → {date(fold.training_end)}</span></li>
            <li><strong>Embargo</strong><span>{fold.embargo_bars} bars excluded</span></li>
            <li><strong>Validate</strong><span>{date(fold.validation_start)} → {date(fold.validation_end)}</span></li>
            <li><strong>Outer holdout</strong><span>Separate from search evidence</span></li>
          </ol>
          <p>Normal risk-adjusted ratio {score(fold.normal?.ratio)} · Stressed ratio {score(fold.stressed?.ratio)}. Selected costs: {result?.trades ?? "—"} trades · Turnover {formatNum(result?.turnover, 4)} · Net P&amp;L {result?.net_profit_usd == null ? "Unavailable" : formatUsd(result.net_profit_usd)}.</p>
          <ChannelStrip label="FOLD EQUITY · INDEXED" series={series} color="var(--color-trace-amber)" overlaySeries={fold.benchmark?.points?.length ? fold.benchmark.points.map(p => ({ x: Date.parse(p.ts), y: p.value })) : null} overlayColor="var(--color-trace-cyan)" overlayLabel={fold.benchmark?.points?.length ? "S&P 500 price index" : null} height={220} emptyMessage="Equity evidence unavailable" currentValueLabel={series.length ? formatNum(series.at(-1).y, 2) : "—"} />
          <div aria-label="Filled order timeline">
            <p>Filled orders over the validation interval · buy above the axis, sell below</p>
            <svg viewBox="0 0 1000 64" role="img" aria-label={`${fills.length} captured filled orders; inspect the table for exact times and prices`}>
              <line x1="0" y1="32" x2="1000" y2="32" stroke="var(--color-hairline)" />
              {fills.filter(f => Number.isFinite(fillTime(f))).map((fill, i) => <circle key={i} cx={end > begin ? 4 + Math.max(0, Math.min(992, (fillTime(fill) - begin) / (end - begin) * 992)) : 4} cy={String(fill.side).toUpperCase() === "BUY" ? 20 : 44} r="4" fill={String(fill.side).toUpperCase() === "BUY" ? "var(--color-trace-cyan)" : "var(--color-trace-amber)"}><title>{fill.side} · {fill.ts_last || fill.ts_event} · {fill.instrument_id} · {fill.avg_px}</title></circle>)}
            </svg>
          </div>
          <p>Equity indexed to 100 at its first captured point. {evidence?.equity_sampled ? `${points.length} sampled points of ${evidence.equity_total}; intrafold extremes may be omitted.` : `${points.length} captured points.`} Metrics use complete evaluation data.</p>
          <p>S&amp;P 500 price index · Price return, dividends excluded · {fold.benchmark?.status || "unavailable"}. {fold.benchmark?.message} {fold.benchmark?.coverage_start && `${date(fold.benchmark.coverage_start)} → ${date(fold.benchmark.coverage_end)}`}</p>
          {evidence?.error && <p role="alert">Capture failed: {evidence.error}</p>}
          <details><summary>Filled orders · {evidence?.fills?.total ?? "unavailable"}{evidence?.fills?.truncated ? " · latest 200 shown" : ""}</summary>
            <p>Filled-order report, capped to the latest 200 orders. Partial executions may be aggregated within an order; signal and trade rationales are not inferred.</p>
            <div className="optimization-evidence__scroll"><table><thead><tr>{Object.keys(evidence?.fills?.rows?.[0] || {}).map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>{(evidence?.fills?.rows || []).map((row, i) => <tr key={i}>{Object.values(row).map((v, j) => <td key={j}>{String(v ?? "—")}</td>)}</tr>)}</tbody></table></div>
          </details>
        </>}
        <details><summary>Score components {detail.state === "running" || detail.state === "pruned" ? "· partial folds" : ""}</summary>
          <p>Median ratio minus dispersion, turnover, and cost penalties. The completed trial must also pass the positive-fold requirement and execute trades in every fold under both cost assumptions.</p>
          <dl>{Object.entries(detail.score_components || {}).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{formatNum(value, 4)}</dd></div>)}</dl>
        </details>
      </>}
    </>}
  </section>;
}
