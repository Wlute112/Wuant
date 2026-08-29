import "./instrument-bezel.css";

export function WorkflowMenuButton({
  menuOpen = false,
  onMenuToggle,
}) {
  return (
    <button
      id="workflow-menu-trigger"
      type="button"
      className={`workspace-menu ${menuOpen ? "is-open" : ""}`}
      aria-label={menuOpen ? "Close workflow menu" : "Open workflow menu"}
      aria-controls="workflow-drawer"
      aria-expanded={menuOpen}
      onClick={onMenuToggle}
    >
      <span aria-hidden="true" />
      <span aria-hidden="true" />
      <span aria-hidden="true" />
    </button>
  );
}

export function BrokerStatus({ runningJobCount = 0, brokerStatus = {} }) {
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
    <div
      className={`broker-status is-${brokerState}`}
      role="status"
      aria-live="polite"
      aria-label={statusLabel}
      title={statusLabel}
    >
      <span
        aria-hidden="true"
        className={`broker-status__dot ${brokerState === "connected" ? "is-active" : ""}`}
      />
      <span className="broker-status__label label">{statusLabel}</span>
    </div>
  );
}
