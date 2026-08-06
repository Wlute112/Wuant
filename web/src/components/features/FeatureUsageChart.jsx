import "./feature-usage-chart.css";

const STATE_LABELS = { off: "OFF", fit: "FIT", raw: "RAW", search: "OPTUNA" };

/**
 * Shows every alpha feature block the Optuna/backtest run can use -- not
 * just log returns -- and whether each is off, fit (jointly weighted inside
 * the Huber regression), raw (bypasses the fit, contributes directly to yhat
 * at its own scale), or search (left for Optuna to tune every trial instead
 * of a fixed manual value). See models/prediction_engine.py's PredictionConfig
 * and optimize/optimize.py's make_objective for the fit/raw/search split.
 */
export default function FeatureUsageChart({ features, searchModes }) {
  const arSearch = searchModes?.n_lags === "optuna";
  const crossAsSearch = searchModes?.cross_asset_lags === "optuna";
  const spreadSearch = searchModes?.spread_lags === "optuna";

  const rows = [
    {
      key: "ar",
      name: "AR (lagged log returns)",
      detail: arSearch ? "optuna-tuned each trial (3-15 lags)" : `n_lags = ${features.n_lags}`,
      state: arSearch ? "search" : features.n_lags > 0 ? "fit" : "off",
    },
    {
      key: "regime",
      name: "Regime (transition-matrix)",
      detail: features.use_regime_features ? `source: ${features.regime_source}` : "off",
      state: features.use_regime_features ? features.regime_source : "off",
    },
    {
      key: "hmm",
      name: "HMM (latent state)",
      detail: features.use_hmm_feature ? `source: ${features.hmm_source}` : "off",
      state: features.use_hmm_feature ? features.hmm_source : "off",
    },
    {
      key: "cross",
      name: "Cross-asset (ARDL + spread)",
      detail: [
        crossAsSearch ? "ARDL: optuna-tuned (0-5)" : `ARDL lags = ${features.cross_asset_lags}`,
        spreadSearch ? "spread: optuna-tuned (0-5)" : `spread lags = ${features.spread_lags}`,
      ].join(", "),
      state:
        crossAsSearch || spreadSearch
          ? "search"
          : features.cross_asset_lags > 0 || features.spread_lags > 0
            ? "fit"
            : "off",
    },
  ];

  return (
    <section className="feature-usage-chart" aria-labelledby="alpha-feature-blocks-title">
      <h3 id="alpha-feature-blocks-title" className="label feature-usage-chart__heading">
        Alpha Feature Blocks
      </h3>
      {rows.map((row) => (
        <div className="feature-usage-chart__row" key={row.key}>
          <span className="feature-usage-chart__name">{row.name}</span>
          <div className="feature-usage-chart__bar-track" aria-hidden="true">
            <div className={`feature-usage-chart__bar is-${row.state}`} />
          </div>
          <span className={`label feature-usage-chart__state is-${row.state}`}>
            {STATE_LABELS[row.state]}
          </span>
          <span className="feature-usage-chart__detail">{row.detail}</span>
        </div>
      ))}
    </section>
  );
}
