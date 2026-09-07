export function operationResult(command) {
  const result = command?.result;
  if (command?.status === "CANCELLED") return "Cancelled before the strategy claimed the request.";
  if (result?.error) return result.error;
  if (result?.blockers?.length) return result.blockers.join(" ");
  if (result?.broker_flat_confirmed === true) return "Broker confirmed: no open positions or working orders.";
  if (command?.status === "ACKNOWLEDGED") return "Accepted by strategy; broker confirmation pending.";
  if (result?.entries_allowed === true) return "Strategy health checks passed. Entries are enabled.";
  if (result?.entries_allowed === false) return "Entries are frozen. Resting-entry cancellation has been requested.";
  if (command?.status === "PENDING") return "Waiting for the strategy to claim this request.";
  if (command?.status === "CLAIMED") return "The strategy is processing this request.";
  return "Review execution and broker telemetry for the resulting state.";
}

export function validOperationsSnapshot(state, jobId) {
  return Boolean(state && state.job_id === jobId && typeof state.heartbeat_fresh === "boolean"
    && Array.isArray(state.resume_blockers) && state.resume_blockers.every((item) => typeof item === "string")
    && Array.isArray(state.commands) && state.commands.every((item) => item && typeof item.command_id === "string"));
}
