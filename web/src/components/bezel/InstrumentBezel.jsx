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

export function BrokerStatus({ runningJobCount = 0, brokerStatus = {}, apiHealth = {} }) {
  const brokerState = brokerStatus.status || "loading";
  const brokerLabel =
    brokerState === "connected"
      ? `IBKR connected${runningJobCount > 0 ? ` · ${runningJobCount} active` : ""}`
      : brokerState === "connecting"
        ? "IBKR connecting"
        : brokerState === "error"
          ? "IBKR error"
          : brokerState === "unknown" || brokerState === "loading"
            ? "IBKR status unknown"
            : "IBKR disconnected";
  const registryState = apiHealth.status === "ok" && apiHealth.job_registry === "redis"
    ? "durable"
    : apiHealth.status === "loading"
      ? "loading"
      : "unknown";
  const registryLabel = registryState === "durable"
    ? "Jobs durable"
    : registryState === "loading"
      ? "Registry checking"
      : "Registry unknown";
  const statusLabel = `${brokerLabel}. ${registryLabel}.`;

  return (
    <div
      className={`broker-status is-${brokerState}`}
      role="status"
      aria-live="polite"
      aria-label={statusLabel}
      title={statusLabel}
    >
      <span className={`broker-status__segment is-${brokerState}`} aria-hidden="true">
        <span className={`broker-status__dot ${brokerState === "connected" ? "is-active" : ""}`} />
        <span className="broker-status__label label">{brokerLabel}</span>
      </span>
      <span className="broker-status__divider" aria-hidden="true" />
      <span className={`broker-status__segment is-${registryState}`} aria-hidden="true">
        <span className={`broker-status__dot ${registryState === "durable" ? "is-durable" : ""}`} />
        <span className="broker-status__label label">{registryLabel}</span>
      </span>
    </div>
  );
}
