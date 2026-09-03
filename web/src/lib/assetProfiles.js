export const FALLBACK_ASSET_PROFILES = {
  crypto: {
    asset_class: "crypto",
    label: "Crypto spot",
    short_label: "CRYPTO",
    description: "Continuous 24/7 spot market with fractional coin sizing.",
    scoring: { metric: "sortino", label: "Sortino ratio", short_label: "SORTINO" },
    market: {
      calendar: "24/7 continuous",
      session: "All available hours",
      venue: "ZEROHASH",
      quantity: "Fractional coins",
      price_type: "MID",
      fee_model: "Zero Hash trailing-volume tiers",
    },
    defaults: {
      tickers: ["BTC", "ETH", "SOL", "XRP", "DOGE"],
      csv: "quant/data/ibkr_bars.csv",
      bar_hours: 24,
      target_score: 1.5,
      regime_window: 20,
      regime_bull_threshold: 0.02,
      regime_bear_threshold: -0.02,
    },
    warnings: [
      "IBKR paper accounts do not support spot-crypto execution.",
      "Live IBKR spot crypto is long-only in this strategy.",
    ],
  },
  equity: {
    asset_class: "equity",
    label: "US equities / ETFs",
    short_label: "EQUITY",
    description: "SMART-routed whole-share instruments on exchange sessions.",
    scoring: { metric: "sharpe", label: "Sharpe ratio", short_label: "SHARPE" },
    market: {
      calendar: "US exchange calendar",
      session: "Regular trading hours",
      venue: "SMART",
      quantity: "Whole shares",
      price_type: "LAST",
      fee_model: "Per-share commission approximation",
    },
    defaults: {
      tickers: ["QQQ"],
      csv: "quant/data/equity_bars.csv",
      bar_hours: 24,
      target_score: 1.0,
      regime_window: 20,
      regime_bull_threshold: 0.01,
      regime_bear_threshold: -0.01,
    },
    warnings: [
      "Whole-share rounding can suppress trades in small allocations.",
      "Extended-hours bars and orders are opt-in and have thinner liquidity.",
      "Overnight gaps can cross an ATR risk reference before the next bar.",
      "Shorts require live IBKR borrow, fee, margin, Rule-201, and what-if approval.",
    ],
  },
};

export function profileMap(profiles) {
  if (!Array.isArray(profiles)) return FALLBACK_ASSET_PROFILES;
  return profiles.reduce(
    (result, profile) => ({ ...result, [profile.asset_class]: profile }),
    { ...FALLBACK_ASSET_PROFILES },
  );
}

export function assetProfile(assetClass, profiles = FALLBACK_ASSET_PROFILES) {
  return profiles[assetClass] || FALLBACK_ASSET_PROFILES.crypto;
}

export function regimeWindowForBarHours(assetClass, barHours, includeExtendedHours = false) {
  const hours = Math.max(1, Number(barHours) || 24);
  if (hours >= 24) return 20;
  const sessionHours = assetClass === "crypto" ? 24 : includeExtendedHours ? 16 : 6.5;
  return 20 * Math.ceil(sessionHours / hours);
}
