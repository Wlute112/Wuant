"""Pydantic request models for the dashboard's job-trigger endpoints."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# The exact phrase a caller must send to /api/jobs/live. Deliberately long and
# unambiguous -- this is the dashboard's "type to arm" safeguard for the one
# action that can deploy real capital (see run/run_live.py's own --live /
# paper-port guard, enforced again here so a UI bug can never bypass it).
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS DEPLOYS REAL CAPITAL"


class FeatureConfig(BaseModel):
    """Which alpha feature blocks are on, and fit-vs-raw source for the two
    regime features (see models/prediction_engine.py's PredictionConfig).
    All optional -- an unset field means "use the engine's own default",
    letting a caller override just the one knob it cares about.
    """
    n_lags: int | None = None  # AR (lagged log-return) block; 0 disables it
    cross_asset_lags: int | None = None
    spread_lags: int | None = None
    use_regime_features: bool | None = None
    use_hmm_feature: bool | None = None
    regime_source: str | None = None  # "fit" | "raw"
    hmm_source: str | None = None     # "fit" | "raw"
    regime_raw_scale: float | None = None
    hmm_raw_scale: float | None = None

    def as_overrides(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class RiskConfigOverrides(BaseModel):
    """Editable risk rails (see strategies/risk.py's RiskConfig). Optional --
    an unset field keeps RiskConfig's own fixed default."""
    risk_budget_pct: float | None = None
    max_trade_risk_pct: float | None = None
    max_leverage: float | None = None
    daily_loss_limit_pct: float | None = None
    kill_switch_pct: float | None = None
    kill_warn_pct: float | None = None
    kelly_max_fraction: float | None = None

    def as_overrides(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class IbkrFetchOptions(BaseModel):
    fetch_missing: bool = False
    replace_bars: bool = False
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1
    ibkr_years: int = 5
    # Only used by replace_bars. Missing-ticker fetches inherit the CSV's
    # existing frequency and never mix frequencies into one file.
    ibkr_bar_hours: int | None = None


class BacktestJobRequest(BaseModel):
    csv: str = "quant/data/sample_bars.csv"
    asset_class: str = "crypto"
    tickers: list[str] | None = None
    cash: float = 5000.0
    params_path: str | None = None
    # Inline base hyperparameters -- e.g. an Optuna run's best_params, fetched
    # client-side via GET /api/runs/{run_id} and sent here so a backtest can
    # be re-run with the tuned values that sweep achieved. Takes precedence
    # over params_path when both are given (see jobs_routes._write_params_file).
    params: dict | None = None
    features: FeatureConfig = FeatureConfig()
    risk: RiskConfigOverrides = RiskConfigOverrides()
    ibkr: IbkrFetchOptions = IbkrFetchOptions()


class OptimizeJobRequest(BaseModel):
    csv: str = "quant/data/sample_bars.csv"
    asset_class: str = "crypto"
    tickers: list[str] | None = None
    trials: int | None = None
    score: float | None = None
    train_frac: float = 0.7
    cash: float = 5000.0
    seed: int | None = None
    warmup_bars: int | None = None
    min_train_bars: int | None = None
    # Continue a prior sweep's Optuna study instead of starting fresh -- see
    # optimize.py's --resume-run-id. Pass the prior run's run_id.
    resume_run_id: str | None = None
    features: FeatureConfig = FeatureConfig()
    risk: RiskConfigOverrides = RiskConfigOverrides()
    ibkr: IbkrFetchOptions = IbkrFetchOptions()


class PaperJobRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1)
    asset_class: Literal["crypto", "equity"] = "crypto"
    primary_exchange: str = ""
    allow_shorts: bool = False
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    account_id: str | None = None
    # Paper risk sizing uses the broker account balance. A manual allocation
    # is intentionally not accepted for paper sessions.
    cash: float | None = None
    params_path: str | None = None
    params: dict | None = None
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379


class LiveJobRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1)
    asset_class: Literal["crypto", "equity"] = "crypto"
    primary_exchange: str = ""
    allow_shorts: bool = False
    host: str = "127.0.0.1"
    port: int
    client_id: int = 1
    account_id: str | None = None
    cash: float = 5000.0
    params_path: str | None = None
    params: dict | None = None
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    confirm: str
