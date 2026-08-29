import FeatureUsageChart from "./FeatureUsageChart.jsx";
import "./feature-panel.css";

export const DEFAULT_FEATURES = {
  n_lags: 5,
  cross_asset_lags: 0,
  spread_lags: 0,
  use_regime_features: true,
  use_hmm_feature: true,
  regime_source: "fit",
  hmm_source: "fit",
  regime_raw_scale: 1.0,
  hmm_raw_scale: 1.0,
  regime_window: 20,
  regime_bull_threshold: 0.02,
  regime_bear_threshold: -0.02,
};

// AR lags / cross-asset ARDL lags / cross-asset spread lags are Optuna's
// ORIGINAL tuned search dimensions (see optimize.py's make_objective --
// trial.suggest_int on all three). Manually pinning them here is an
// ADDITIONAL option on top of that, so an Optuna sweep defaults to letting
// Optuna search each one, matching the CLI's original behavior.
export const DEFAULT_SEARCH_MODES = {
  n_lags: "optuna",
  cross_asset_lags: "optuna",
  spread_lags: "optuna",
};

/** Controlled feature-toggle form + live usage chart. `value`/`onChange`
 * carry the full FeatureConfig the request body sends -- see api/schemas.py.
 * `searchModes`/`onSearchModesChange` (only meaningful when `allowOptunaSearch`
 * is true, i.e. the Optuna sweep tab) choose per-field whether Optuna tunes
 * n_lags/cross_asset_lags/spread_lags itself or the manual value below is
 * pinned as a structural override for every trial. */
export default function FeaturePanel({
  value,
  onChange,
  searchModes = DEFAULT_SEARCH_MODES,
  onSearchModesChange,
  allowOptunaSearch = false,
}) {
  function set(patch) {
    onChange({ ...value, ...patch });
  }

  function setMode(key, mode) {
    onSearchModesChange({ ...searchModes, [key]: mode });
  }

  function renderModeToggle(fieldKey, fieldLabel) {
    if (!allowOptunaSearch) return null;
    return (
      <select
        className="feature-panel__mode"
        aria-label={`${fieldLabel} configuration mode`}
        value={searchModes[fieldKey]}
        onChange={(e) => setMode(fieldKey, e.target.value)}
      >
        <option value="optuna">Optuna-tuned</option>
        <option value="manual">Manual</option>
      </select>
    );
  }

  const arManual = !allowOptunaSearch || searchModes.n_lags === "manual";
  const crossAsManual = !allowOptunaSearch || searchModes.cross_asset_lags === "manual";
  const spreadManual = !allowOptunaSearch || searchModes.spread_lags === "manual";

  return (
    <div className="feature-panel">
      <FeatureUsageChart features={value} searchModes={allowOptunaSearch ? searchModes : undefined} />

      <div className="feature-panel__controls">
        <fieldset className="feature-panel__field">
          <legend className="label">AR lags (0 = off)</legend>
          {renderModeToggle("n_lags", "AR lags")}
          <input
            aria-label="AR lag count"
            type="number"
            min={0}
            max={30}
            disabled={!arManual}
            value={value.n_lags}
            onChange={(e) => set({ n_lags: Number(e.target.value) })}
          />
        </fieldset>

        <fieldset className="feature-panel__field">
          <legend className="label">Cross-asset ARDL lags</legend>
          {renderModeToggle("cross_asset_lags", "Cross-asset ARDL lags")}
          <input
            aria-label="Cross-asset ARDL lag count"
            type="number"
            min={0}
            max={10}
            disabled={!crossAsManual}
            value={value.cross_asset_lags}
            onChange={(e) => set({ cross_asset_lags: Number(e.target.value) })}
          />
        </fieldset>

        <fieldset className="feature-panel__field">
          <legend className="label">Cross-asset spread lags</legend>
          {renderModeToggle("spread_lags", "Cross-asset spread lags")}
          <input
            aria-label="Cross-asset spread lag count"
            type="number"
            min={0}
            max={10}
            disabled={!spreadManual}
            value={value.spread_lags}
            onChange={(e) => set({ spread_lags: Number(e.target.value) })}
          />
        </fieldset>

        <div className="feature-panel__block">
          <label className="feature-panel__checkbox">
            <input
              type="checkbox"
              checked={value.use_regime_features}
              onChange={(e) => set({ use_regime_features: e.target.checked })}
            />
            <span className="label">Regime (transition-matrix)</span>
          </label>
          {value.use_regime_features && (
            <>
              <div className="feature-panel__sub">
                <select
                  aria-label="Regime feature source"
                  value={value.regime_source}
                  onChange={(e) => set({ regime_source: e.target.value })}
                >
                  <option value="fit">Fit (weighted in Huber)</option>
                  <option value="raw">Raw (bypass Huber)</option>
                </select>
                {value.regime_source === "raw" && (
                  <input
                    aria-label="Regime raw contribution scale"
                    type="number"
                    step={0.01}
                    value={value.regime_raw_scale}
                    onChange={(e) => set({ regime_raw_scale: Number(e.target.value) })}
                    title="regime_raw_scale -- yhat contribution = regime_score * this"
                  />
                )}
              </div>
              <div className="feature-panel__regime-parameters">
                <input
                  aria-label="Regime lookback bars"
                  type="number"
                  min={2}
                  value={value.regime_window}
                  onChange={(e) => set({ regime_window: Number(e.target.value) })}
                  title="Regime lookback bars"
                />
                <input
                  aria-label="Bull regime return threshold"
                  type="number"
                  min={0.0001}
                  step={0.001}
                  value={value.regime_bull_threshold}
                  onChange={(e) => set({ regime_bull_threshold: Number(e.target.value) })}
                  title="Bull return threshold"
                />
                <input
                  aria-label="Bear regime return threshold"
                  type="number"
                  max={-0.0001}
                  step={0.001}
                  value={value.regime_bear_threshold}
                  onChange={(e) => set({ regime_bear_threshold: Number(e.target.value) })}
                  title="Bear return threshold"
                />
              </div>
              <span className="feature-panel__hint">Window · bull threshold · bear threshold</span>
            </>
          )}
        </div>

        <div className="feature-panel__block">
          <label className="feature-panel__checkbox">
            <input
              type="checkbox"
              checked={value.use_hmm_feature}
              onChange={(e) => set({ use_hmm_feature: e.target.checked })}
            />
            <span className="label">HMM (latent state)</span>
          </label>
          {value.use_hmm_feature && (
            <div className="feature-panel__sub">
              <select
                aria-label="HMM feature source"
                value={value.hmm_source}
                onChange={(e) => set({ hmm_source: e.target.value })}
              >
                <option value="fit">Fit (weighted in Huber)</option>
                <option value="raw">Raw (bypass Huber)</option>
              </select>
              {value.hmm_source === "raw" && (
                <input
                  aria-label="HMM raw contribution scale"
                  type="number"
                  step={0.01}
                  value={value.hmm_raw_scale}
                  onChange={(e) => set({ hmm_raw_scale: Number(e.target.value) })}
                  title="hmm_raw_scale -- yhat contribution = hmm_signed * this"
                />
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
