import "./instrument-bezel.css";

export default function InstrumentBezel({ runningJobCount, brokerStatus = {} }) {
  const brokerState = brokerStatus.status || "loading";
  const statusLabel =
    brokerState === "connected"
      ? `IBKR connected${runningJobCount > 0 ? ` · ${runningJobCount} job${runningJobCount === 1 ? "" : "s"}` : ""}`
      : brokerState === "connecting"
        ? "IBKR connecting"
        : brokerState === "error"
          ? "IBKR error"
        : brokerState === "unknown" || brokerState === "loading"
          ? "IBKR status unknown"
          : "IBKR disconnected";

  return (
    <header className="instrument-bezel">
      <h1 className="instrument-bezel__title display">STRIP RECORDER</h1>
      <div className="label instrument-bezel__subtitle">Quant Trading System — Reporting Dashboard</div>
      <div
        className={`instrument-bezel__status is-${brokerState}`}
        role="status"
        aria-live="polite"
      >
        <span
          aria-hidden="true"
          className={`instrument-bezel__dot ${brokerState === "connected" ? "is-active" : ""}`}
        />
        <span className="label">{statusLabel}</span>
      </div>
    </header>
  );
}
