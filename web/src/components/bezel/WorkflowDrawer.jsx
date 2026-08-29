import { useEffect, useRef } from "react";

import "./workflow-drawer.css";

const WORKFLOW_GROUPS = [
  {
    label: "Research",
    modes: [
      {
        key: "backtest",
        label: "Backtest",
        description: "Historical execution and run review",
      },
      {
        key: "optimize",
        label: "Optuna sweep",
        description: "Profile-aware parameter search",
      },
    ],
  },
  {
    label: "Broker execution",
    modes: [
      {
        key: "paper",
        label: "Paper trading",
        description: "IBKR simulation · equities only",
      },
      {
        key: "live",
        label: "Live trading",
        description: "IBKR execution · real capital",
        danger: true,
      },
    ],
  },
];

export default function WorkflowDrawer({
  open,
  activeWorkflow,
  runningJobCount = 0,
  onClose,
  onSelect,
}) {
  const drawerRef = useRef(null);
  const restoreFocusRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;

    restoreFocusRef.current = document.activeElement;
    const drawer = drawerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    drawer?.querySelector(`[data-workflow="${activeWorkflow}"]`)?.focus();

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !drawer) return;

      const focusable = Array.from(
        drawer.querySelectorAll('button:not([disabled]), [href], input, select, [tabindex]:not([tabindex="-1"])'),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      const returnTarget = restoreFocusRef.current;
      if (
        returnTarget instanceof HTMLElement &&
        returnTarget !== document.body &&
        !drawer?.contains(returnTarget)
      ) {
        returnTarget.focus();
      } else {
        document.getElementById("workflow-menu-trigger")?.focus();
      }
    };
  }, [activeWorkflow, onClose, open]);

  return (
    <div className={`workflow-shell ${open ? "is-open" : ""}`}>
      <div className="workflow-shell__backdrop" aria-hidden="true" onClick={onClose} />
      <aside
        ref={drawerRef}
        id="workflow-drawer"
        className="workflow-drawer"
        role="dialog"
        aria-modal="true"
        aria-hidden={!open}
        aria-labelledby="workflow-drawer-title"
      >
        <div className="workflow-drawer__header">
          <div>
            <p className="label">System mode</p>
            <h2 id="workflow-drawer-title" className="workflow-drawer__title display">
              Select workspace
            </h2>
          </div>
          <button
            type="button"
            className="workflow-drawer__close"
            aria-label="Close workflow menu"
            tabIndex={open ? 0 : -1}
            onClick={onClose}
          >
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </button>
        </div>

        <nav className="workflow-drawer__nav" aria-label="Trading and research modes">
          {WORKFLOW_GROUPS.map((group) => {
            const groupId = `workflow-group-${group.label.replaceAll(" ", "-").toLowerCase()}`;
            return (
              <section
                className="workflow-drawer__group"
                key={group.label}
                aria-labelledby={groupId}
              >
                <h3
                  id={groupId}
                  className="workflow-drawer__group-label label"
                >
                  {group.label}
                </h3>
                <div className="workflow-drawer__items">
                  {group.modes.map((mode) => {
                    const active = activeWorkflow === mode.key;
                    return (
                      <button
                        key={mode.key}
                        type="button"
                        data-workflow={mode.key}
                        className={`workflow-drawer__item ${active ? "is-active" : ""} ${mode.danger ? "is-danger" : ""}`}
                        aria-current={active ? "page" : undefined}
                        tabIndex={open ? 0 : -1}
                        onClick={() => onSelect(mode.key)}
                      >
                        <span className="workflow-drawer__item-copy">
                          <span className="workflow-drawer__item-label">{mode.label}</span>
                          <span className="workflow-drawer__item-description">
                            {mode.description}
                          </span>
                        </span>
                        <span
                          className="workflow-drawer__item-state label"
                          aria-hidden="true"
                        >
                          {active ? "Active" : "Open"}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </nav>

        <div className="workflow-drawer__footer">
          <p>
            Research results and broker telemetry stay in separate workspaces so live positions never crowd backtest review.
          </p>
          <span className="workflow-drawer__jobs label" role="status">
            {runningJobCount > 0
              ? `${runningJobCount} active job${runningJobCount === 1 ? "" : "s"}`
              : "No active jobs"}
          </span>
        </div>
      </aside>
    </div>
  );
}
