import { useEffect, useMemo, useRef, useState } from "react";
import GridLayout from "react-grid-layout";

import { DASHBOARD_THEMES } from "../../lib/theme.js";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import "./dock-workspace.css";

const COLS = 12;
const ROWS = 12;
const STORAGE_PREFIX = "quant-dashboard.workspace.v2";

const DENSITIES = {
  compact: { label: "Compact", gap: 4 },
  balanced: { label: "Balanced", gap: 6 },
  comfortable: { label: "Comfortable", gap: 8 },
};

function savedWorkspace(workspaceId) {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(`${STORAGE_PREFIX}.${workspaceId}`));
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function defaultLayout(panels) {
  return panels.map((panel) => ({
    i: panel.id,
    ...panel.defaultLayout,
    minW: panel.minW || 2,
    minH: panel.minH || 2,
    maxW: panel.maxW || COLS,
    maxH: panel.maxH || ROWS,
  }));
}

function mergeLayout(panels, stored) {
  const defaults = defaultLayout(panels);
  const byId = new Map((stored?.layout || []).map((item) => [item.i, item]));
  return defaults.map((item) => {
    const saved = byId.get(item.i);
    if (!saved) return item;
    return {
      ...item,
      x: Math.max(0, Math.min(COLS - item.minW, Number(saved.x) || 0)),
      y: Math.max(0, Math.min(ROWS - item.minH, Number(saved.y) || 0)),
      w: Math.max(item.minW, Math.min(item.maxW, Number(saved.w) || item.w)),
      h: Math.max(item.minH, Math.min(item.maxH, Number(saved.h) || item.h)),
    };
  });
}

function mergeVisibility(panels, stored) {
  const saved = stored?.visible;
  return Object.fromEntries(
    panels.map((panel) => [panel.id, saved?.[panel.id] !== false]),
  );
}

function useElementSize(ref) {
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const update = () => {
      const rect = node.getBoundingClientRect();
      setSize({ width: Math.floor(rect.width), height: Math.floor(rect.height) });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);

  return size;
}

function fillVacancy(layout, removedId, visibility) {
  const removed = layout.find((item) => item.i === removedId);
  if (!removed) return layout;
  const candidates = layout.filter(
    (item) => item.i !== removedId && visibility[item.i] !== false,
  );
  const sameRows = (item) => item.y === removed.y && item.h === removed.h;
  const sameColumns = (item) => item.x === removed.x && item.w === removed.w;
  const right = candidates.find((item) => sameRows(item) && item.x === removed.x + removed.w);
  const left = candidates.find((item) => sameRows(item) && item.x + item.w === removed.x);
  const below = candidates.find((item) => sameColumns(item) && item.y === removed.y + removed.h);
  const above = candidates.find((item) => sameColumns(item) && item.y + item.h === removed.y);
  const neighbor = right || left || below || above;
  if (!neighbor) return layout;
  const expanded = right
    ? { ...neighbor, x: removed.x, w: neighbor.w + removed.w }
    : left
      ? { ...neighbor, w: neighbor.w + removed.w }
      : below
        ? { ...neighbor, y: removed.y, h: neighbor.h + removed.h }
        : { ...neighbor, h: neighbor.h + removed.h };
  if (expanded.w > expanded.maxW || expanded.h > expanded.maxH) return layout;
  return layout.map((item) => (item.i === neighbor.i ? expanded : item));
}

export default function DockWorkspace({
  workspaceId,
  title,
  subtitle,
  panels,
  toolbarNavigation = null,
  toolbarLead = null,
  toolbarActions = null,
  toolbarStatus = null,
  status = null,
  theme = "dark",
  onThemeChange = null,
}) {
  const initial = useMemo(() => savedWorkspace(workspaceId), [workspaceId]);
  const [layout, setLayout] = useState(() => mergeLayout(panels, initial));
  const [visible, setVisible] = useState(() => mergeVisibility(panels, initial));
  const [density, setDensity] = useState(
    () => (DENSITIES[initial?.density] ? initial.density : "compact"),
  );
  const [panelMenuOpen, setPanelMenuOpen] = useState(false);
  const canvasRef = useRef(null);
  const { width, height } = useElementSize(canvasRef);
  const narrow = width > 0 && width < 700;
  const gap = DENSITIES[density].gap;
  const rowHeight = Math.max(24, Math.floor((height - gap * (ROWS - 1)) / ROWS));
  const visiblePanels = panels.filter((panel) => visible[panel.id] !== false);
  const hiddenPanels = panels.filter((panel) => visible[panel.id] === false);
  const liveLayout = layout.filter((item) => visible[item.i] !== false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        window.localStorage.setItem(
          `${STORAGE_PREFIX}.${workspaceId}`,
          JSON.stringify({ layout, visible, density }),
        );
      } catch {
        // The workspace stays fully usable when local persistence is blocked.
      }
    }, 120);
    return () => window.clearTimeout(timer);
  }, [density, layout, visible, workspaceId]);

  useEffect(() => {
    function closeMenu(event) {
      if (!event.target.closest(".dock-workspace__panel-menu")) setPanelMenuOpen(false);
    }
    if (panelMenuOpen) document.addEventListener("pointerdown", closeMenu);
    return () => document.removeEventListener("pointerdown", closeMenu);
  }, [panelMenuOpen]);

  function updateLayout(nextLayout) {
    setLayout((current) => {
      const nextById = new Map(nextLayout.map((item) => [item.i, item]));
      return current.map((item) => {
        const next = nextById.get(item.i);
        return next ? { ...item, x: next.x, y: next.y, w: next.w, h: next.h } : item;
      });
    });
  }

  function hidePanel(panelId) {
    setLayout((current) => fillVacancy(current, panelId, visible));
    setVisible((current) => ({ ...current, [panelId]: false }));
  }

  function showPanel(panelId) {
    setVisible((current) => ({ ...current, [panelId]: true }));
    setPanelMenuOpen(false);
  }

  function resetLayout() {
    setLayout(defaultLayout(panels));
    setVisible(Object.fromEntries(panels.map((panel) => [panel.id, true])));
    setDensity("compact");
    setPanelMenuOpen(false);
  }

  function keyboardPanelChange(panelId, event) {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    setLayout((current) => current.map((item) => {
      if (item.i !== panelId) return item;
      if (event.shiftKey) {
        const widthDelta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        const heightDelta = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
        return {
          ...item,
          w: Math.max(item.minW, Math.min(item.maxW, item.w + widthDelta)),
          h: Math.max(item.minH, Math.min(item.maxH, item.h + heightDelta)),
        };
      }
      const xDelta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      const yDelta = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
      return {
        ...item,
        x: Math.max(0, Math.min(COLS - item.w, item.x + xDelta)),
        y: Math.max(0, Math.min(ROWS - item.h, item.y + yDelta)),
      };
    }));
  }

  const panelNodes = visiblePanels.map((panel) => (
    <section
      key={panel.id}
      className={`dock-panel dock-panel--${panel.kind || panel.id}`}
      aria-labelledby={`${workspaceId}-${panel.id}-title`}
    >
      <header className="dock-panel__bar">
        <button
          type="button"
          className="dock-panel__grip"
          aria-label={`Move ${panel.title} panel with arrow keys; hold Shift and use arrow keys to resize`}
          onKeyDown={(event) => keyboardPanelChange(panel.id, event)}
        >
          ⠿
        </button>
        <h2 id={`${workspaceId}-${panel.id}-title`}>{panel.title}</h2>
        {panel.reading && <span className="dock-panel__reading num">{panel.reading}</span>}
        <button
          type="button"
          className="dock-panel__remove"
          aria-label={`Remove ${panel.title} panel`}
          title="Remove panel"
          onClick={() => hidePanel(panel.id)}
        >
          ×
        </button>
      </header>
      <div className="dock-panel__body">{panel.content}</div>
    </section>
  ));

  return (
    <section className={`dock-workspace density-${density} ${narrow ? "is-narrow" : ""}`}>
      <header className="dock-workspace__toolbar">
        {toolbarNavigation && <div className="dock-workspace__navigation">{toolbarNavigation}</div>}
        <div className="dock-workspace__identity">
          <div>
            <h1>{title}</h1>
            {subtitle && <p>{subtitle}</p>}
          </div>
          {status}
        </div>
        {toolbarLead && <div className="dock-workspace__lead">{toolbarLead}</div>}
        <div className="dock-workspace__actions">
          {toolbarActions}
          <div className="dock-workspace__theme" role="group" aria-label="Dashboard theme">
            <span>Theme</span>
            <div className="dock-workspace__theme-options">
              {DASHBOARD_THEMES.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={`dock-workspace__theme-option is-${option.id}`}
                  aria-label={`Use ${option.label.toLowerCase()} dashboard theme`}
                  aria-pressed={theme === option.id}
                  title={`${option.label} theme`}
                  onClick={() => onThemeChange?.(option.id)}
                >
                  <span aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
          <label className="dock-workspace__density">
            <span>Density</span>
            <select value={density} onChange={(event) => setDensity(event.target.value)}>
              {Object.entries(DENSITIES).map(([value, option]) => (
                <option key={value} value={value}>{option.label}</option>
              ))}
            </select>
          </label>
          <div className="dock-workspace__panel-menu">
            <button
              type="button"
              aria-haspopup="menu"
              aria-expanded={panelMenuOpen}
              onClick={() => setPanelMenuOpen((open) => !open)}
            >
              Panels{hiddenPanels.length ? ` +${hiddenPanels.length}` : ""}
            </button>
            {panelMenuOpen && (
              <div className="dock-workspace__menu-popover" role="menu">
                <span>Hidden panels</span>
                {hiddenPanels.length ? hiddenPanels.map((panel) => (
                  <button key={panel.id} type="button" role="menuitem" onClick={() => showPanel(panel.id)}>
                    Add {panel.title}
                  </button>
                )) : <em>All panels are visible</em>}
                <button type="button" role="menuitem" onClick={resetLayout}>Reset TWS layout</button>
              </div>
            )}
          </div>
        </div>
        {toolbarStatus && <div className="dock-workspace__status">{toolbarStatus}</div>}
      </header>

      <div ref={canvasRef} className="dock-workspace__canvas">
        {width > 0 && height > 0 && !narrow ? (
          <GridLayout
            className="dock-workspace__grid"
            layout={liveLayout}
            width={width}
            cols={COLS}
            rowHeight={rowHeight}
            margin={[gap, gap]}
            containerPadding={[0, 0]}
            maxRows={ROWS}
            compactType="vertical"
            draggableHandle=".dock-panel__bar"
            draggableCancel="button, input, select, a"
            resizeHandles={["se"]}
            isBounded
            useCSSTransforms
            onLayoutChange={updateLayout}
          >
            {panelNodes}
          </GridLayout>
        ) : (
          <div className="dock-workspace__stack">{panelNodes}</div>
        )}
      </div>
    </section>
  );
}
