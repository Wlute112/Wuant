import { memo, useEffect, useId, useMemo, useRef, useState } from "react";

import { buildPathD, computeDomain, padDomain, TRACE_VIEW_WIDTH, yToPixel } from "../../lib/trace.js";
import "./channel-strip.css";

const GRID_ROWS = 4;
const ZOOM_STEP = 1.2;
const MAX_ZOOM = 6;

/**
 * The dashboard's signature component (see DESIGN.md "Channel Strip"): a
 * left label rail plus a scrolling trace canvas with real threshold bands.
 * `series`/`ghostSeries` are [{x: msTimestamp, y: number}]; `thresholds` are
 * [{value, label, kind: "warn"|"danger"}] positioned at their REAL y-value,
 * never decorative.
 */
function ChannelStrip({
  label,
  color,
  series = [],
  ghostSeries = null,
  ghostLabel = null,
  // A second FULL-opacity trace in its own color -- distinct from
  // ghostSeries (a comparison run, same color, 40% opacity). Used for
  // overlays like actual-vs-predicted price where both traces are equally
  // "real" data, not one primary and one faded reference.
  overlaySeries = null,
  overlayColor = null,
  overlayLabel = null,
  thresholds = [],
  currentValueLabel = "—",
  showRailReading = true,
  height = 140,
  emptyMessage = "No data yet",
  tickFormat = (v) => v.toFixed(2),
}) {
  const pathRef = useRef(null);
  const overlayRef = useRef(null);
  const pathAnimationRef = useRef(null);
  const overlayAnimationRef = useRef(null);
  const traceRef = useRef(null);
  const headingId = useId();
  const descriptionId = useId();
  const [zoom, setZoom] = useState(1);
  const [zoomCenter, setZoomCenter] = useState(0.5);
  const [resetAnimationKey, setResetAnimationKey] = useState(0);
  const [hover, setHover] = useState(null);
  const zoomRef = useRef(zoom);
  const gestureStartRef = useRef(null);

  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  const { domain: baseDomain } = useMemo(() => {
    const allSeries = [series];
    if (ghostSeries) allSeries.push(ghostSeries);
    if (overlaySeries) allSeries.push(overlaySeries);
    if (thresholds.length) {
      const anchorX = series[0]?.x ?? ghostSeries?.[0]?.x ?? 0;
      // Threshold bands render at their REAL value even when the trace never
      // approaches them (that "still far from the rail" gap is the point) --
      // fold them into the y-domain so they never collapse onto the clamped
      // edge and overlap illegibly.
      allSeries.push(thresholds.map((t) => ({ x: anchorX, y: t.value })));
    }
    const nextDomain = padDomain(computeDomain(allSeries));
    return {
      domain: nextDomain,
    };
  }, [ghostSeries, height, overlaySeries, series, thresholds]);

  const domain = useMemo(() => {
    const span = baseDomain.xMax - baseDomain.xMin || 1;
    const visibleSpan = span / zoom;
    const center = baseDomain.xMin + span * zoomCenter;
    const xMin = Math.max(baseDomain.xMin, center - visibleSpan / 2);
    const xMax = Math.min(baseDomain.xMax, center + visibleSpan / 2);
    return { ...baseDomain, xMin, xMax };
  }, [baseDomain, zoom, zoomCenter]);

  const pathD = useMemo(() => buildPathD(series, domain, height), [domain, height, series]);
  const ghostPathD = useMemo(
    () => (ghostSeries ? buildPathD(ghostSeries, domain, height) : null),
    [domain, ghostSeries, height],
  );
  const overlayPathD = useMemo(
    () => (overlaySeries ? buildPathD(overlaySeries, domain, height) : null),
    [domain, height, overlaySeries],
  );

  const visibleSeries = useMemo(
    () => [
      { key: "primary", label, color, points: series },
      ...(ghostSeries ? [{ key: "ghost", label: ghostLabel || "comparison", color, points: ghostSeries }] : []),
      ...(overlaySeries ? [{ key: "overlay", label: overlayLabel || "overlay", color: overlayColor, points: overlaySeries }] : []),
    ],
    [color, label, overlayColor, overlayLabel, overlaySeries, series],
  );

  const nearestPoint = (points, x) => {
    if (!points?.length) return null;
    return points.reduce((nearest, point) =>
      Math.abs(point.x - x) < Math.abs(nearest.x - x) ? point : nearest,
    );
  };

  function formatX(value) {
    if (!Number.isFinite(value)) return "—";
    if (value > 1e11) {
      return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(value);
    }
    return `#${value}`;
  }

  function updateHover(event, seriesKey = null) {
    if (!series.length) return;
    const rect = traceRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const yRatio = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    const x = domain.xMin + (domain.xMax - domain.xMin) * ratio;
    const points = visibleSeries.map((item) => ({ ...item, point: nearestPoint(item.points, x) }));
    setHover({ ratio, yRatio, x, seriesKey, points });
  }

  function zoomBy(factor, center = zoomCenter) {
    setZoom((value) => Math.max(1, Math.min(MAX_ZOOM, value * factor)));
    setZoomCenter(center);
  }

  function resetZoom() {
    setZoom(1);
    setZoomCenter(0.5);
    setResetAnimationKey((value) => value + 1);
  }

  useEffect(() => {
    const node = traceRef.current;
    if (!node) return undefined;

    const clampZoom = (value) => Math.max(1, Math.min(MAX_ZOOM, value));
    const getCenter = (event) => {
      const rect = node.getBoundingClientRect();
      const clientX = Number.isFinite(event.clientX) ? event.clientX : rect.left + rect.width / 2;
      return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    };

    // Chrome reports macOS trackpad pinch as a modified wheel event. The
    // native non-passive listener is intentional: React's delegated wheel
    // listener cannot reliably cancel browser page zoom in every browser.
    const onWheel = (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      event.stopPropagation();
      const center = getCenter(event);
      const step = Math.max(-1, Math.min(1, -event.deltaY / 80));
      setZoom((value) => clampZoom(value * Math.pow(ZOOM_STEP, step)));
      setZoomCenter(center);
    };

    // Safari/WebKit exposes trackpad pinch through gesture events instead of
    // a modified wheel event.
    const onGestureStart = (event) => {
      event.preventDefault();
      gestureStartRef.current = { zoom: zoomRef.current, center: getCenter(event) };
    };
    const onGestureChange = (event) => {
      if (!gestureStartRef.current) return;
      event.preventDefault();
      setZoom(clampZoom(gestureStartRef.current.zoom * event.scale));
      setZoomCenter(gestureStartRef.current.center);
    };
    const onGestureEnd = (event) => {
      event.preventDefault();
      gestureStartRef.current = null;
    };

    node.addEventListener("wheel", onWheel, { passive: false });
    node.addEventListener("gesturestart", onGestureStart, { passive: false });
    node.addEventListener("gesturechange", onGestureChange, { passive: false });
    node.addEventListener("gestureend", onGestureEnd, { passive: false });
    return () => {
      node.removeEventListener("wheel", onWheel);
      node.removeEventListener("gesturestart", onGestureStart);
      node.removeEventListener("gesturechange", onGestureChange);
      node.removeEventListener("gestureend", onGestureEnd);
    };
  }, []);

  const hoverReadout = hover?.points.filter((item) => item.point) || [];
  const activeHover = hoverReadout.find((item) => item.key === hover.seriesKey) || hoverReadout[0];
  const activeHoverY = activeHover?.point ? yToPixel(activeHover.point.y, domain, height) : height / 2;

  function drawOnce(el, d, animationRef) {
    if (!el || !d || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return undefined;
    }
    animationRef.current?.cancel();
    const length = el.getTotalLength();
    el.style.strokeDasharray = `${length}`;
    const animation = el.animate(
      [{ strokeDashoffset: length }, { strokeDashoffset: 0 }],
      { duration: 900, easing: "cubic-bezier(0.16, 1, 0.3, 1)" },
    );
    animationRef.current = animation;
    return () => {
      animation.cancel();
      animationRef.current = null;
    };
  }

  // The pen animation writes a dash length onto the SVG path. Clear that
  // transient state whenever the viewport/path changes so zoomed geometry is
  // rendered as a complete continuous trace instead of inheriting an old
  // dash pattern.
  useEffect(() => {
    [pathRef.current, overlayRef.current].forEach((element) => {
      if (!element) return;
      element.style.strokeDasharray = "none";
      element.style.strokeDashoffset = "0";
    });
    pathAnimationRef.current?.cancel();
    overlayAnimationRef.current?.cancel();
    pathAnimationRef.current = null;
    overlayAnimationRef.current = null;
  }, [pathD, overlayPathD, zoom, zoomCenter]);

  // Zooming changes the viewport geometry in place. Only the initial render
  // and an explicit reset replay the instrument's pen-draw animation.
  useEffect(() => drawOnce(pathRef.current, pathD, pathAnimationRef), [resetAnimationKey]);
  useEffect(() => drawOnce(overlayRef.current, overlayPathD, overlayAnimationRef), [resetAnimationKey]);

  const accessibleSummary =
    series.length === 0
      ? emptyMessage
      : `${series.length} observations. Current reading ${currentValueLabel}. Visible range ${tickFormat(
          domain.yMin,
        )} to ${tickFormat(domain.yMax)}.${
          thresholds.length
            ? ` Risk thresholds: ${thresholds.map((item) => item.label).join(", ")}.`
            : ""
        }`;

  return (
    <section className="channel-strip" aria-labelledby={headingId}>
      <div className="channel-strip__rail">
        <h2 id={headingId} className="label channel-strip__heading">
          {label}
        </h2>
        {showRailReading && <div className="display num" style={{ color }}>{currentValueLabel}</div>}
        {ghostLabel && (
          <div className="label channel-strip__ghost-label">vs {ghostLabel}</div>
        )}
        {overlayLabel && (
          <div className="label channel-strip__overlay-label" style={{ color: overlayColor }}>
            — {overlayLabel}
          </div>
        )}
      </div>
      <p id={descriptionId} className="sr-only">
        {accessibleSummary}
      </p>
      <div
        className="channel-strip__trace grain"
        ref={traceRef}
        style={{ height, "--trace-height": `${height}px` }}
        role="group"
        aria-label={`${label} chart`}
        aria-describedby={descriptionId}
        onMouseMove={updateHover}
        onMouseLeave={() => setHover(null)}
      >
        <div className="channel-strip__tools" aria-label={`${label} chart controls`}>
          <button type="button" onClick={() => zoomBy(1 / ZOOM_STEP)} disabled={zoom === 1} aria-label={`Zoom out ${label}`}>−</button>
          <span className="label" aria-live="polite">{Math.round(zoom * 100)}%</span>
          <button type="button" onClick={() => zoomBy(ZOOM_STEP)} aria-label={`Zoom in ${label}`}>+</button>
          <button type="button" onClick={resetZoom} disabled={zoom === 1} aria-label={`Reset zoom ${label}`}>reset</button>
        </div>
        {series.length === 0 ? (
          <div className="channel-strip__empty">{emptyMessage}</div>
        ) : (
          <svg
            viewBox={`0 0 ${TRACE_VIEW_WIDTH} ${height}`}
            preserveAspectRatio="none"
            className="channel-strip__svg"
            aria-hidden="true"
            focusable="false"
          >
            {Array.from({ length: GRID_ROWS + 1 }).map((_, i) => (
              <line
                key={i}
                x1={0}
                x2={TRACE_VIEW_WIDTH}
                y1={(height / GRID_ROWS) * i}
                y2={(height / GRID_ROWS) * i}
                stroke="var(--color-hairline)"
                strokeWidth={1}
              />
            ))}

            {thresholds.map((t) => {
              const y = yToPixel(t.value, domain, height);
              const bandColor =
                t.kind === "danger" ? "var(--color-threshold-danger)" : "var(--color-threshold-warn)";
              return (
                <g key={t.label}>
                  <rect
                    x={0}
                    y={Math.min(y, height)}
                    width={TRACE_VIEW_WIDTH}
                    height={Math.max(height - y, 0)}
                    fill={bandColor}
                    opacity={0.12}
                  />
                  <line x1={0} x2={TRACE_VIEW_WIDTH} y1={y} y2={y} stroke={bandColor} strokeWidth={1.5} />
                </g>
              );
            })}

            {ghostPathD && (
              <path
                d={ghostPathD}
                fill="none"
                stroke={color}
                strokeWidth={2}
                opacity={0.4}
                className={hover?.seriesKey && hover.seriesKey !== "ghost" ? "is-dimmed" : ""}
              />
            )}

            <path
              ref={pathRef}
              d={pathD}
              fill="none"
              stroke={color}
              strokeWidth={2}
              className={hover?.seriesKey && hover.seriesKey !== "primary" ? "is-dimmed" : ""}
            />

            {overlayPathD && (
              <path
                ref={overlayRef}
                d={overlayPathD}
                fill="none"
                stroke={overlayColor}
                strokeWidth={2}
                className={hover?.seriesKey && hover.seriesKey !== "overlay" ? "is-dimmed" : ""}
              />
            )}

            {ghostPathD && (
              <path
                d={ghostPathD}
                fill="none"
                stroke="transparent"
                strokeWidth={14}
                className="channel-strip__hit-area"
                onMouseEnter={() => setHover((current) => current ? { ...current, seriesKey: "ghost" } : current)}
                onMouseMove={(event) => updateHover(event, "ghost")}
              />
            )}

            {overlayPathD && (
              <>
                <path
                  d={pathD}
                  fill="none"
                  stroke="transparent"
                  strokeWidth={14}
                  className="channel-strip__hit-area"
                  onMouseEnter={() => setHover((current) => current ? { ...current, seriesKey: "primary" } : current)}
                  onMouseMove={(event) => updateHover(event, "primary")}
                />
                <path
                  d={overlayPathD}
                  fill="none"
                  stroke="transparent"
                  strokeWidth={14}
                  className="channel-strip__hit-area"
                  onMouseEnter={() => setHover((current) => current ? { ...current, seriesKey: "overlay" } : current)}
                  onMouseMove={(event) => updateHover(event, "overlay")}
                />
              </>
            )}
          </svg>
        )}

        {hover && (
          <div
            className="channel-strip__tooltip"
            style={{
              left: `clamp(80px, ${hover.ratio * 100}%, calc(100% - 100px))`,
              ...(hover.yRatio < 0.45
                ? { top: `${Math.min(68, hover.yRatio * 100 + 7)}%` }
                : { bottom: `${Math.min(68, (1 - hover.yRatio) * 100 + 7)}%` }),
              transform: "translateX(-50%)",
            }}
            role="status"
            aria-live="polite"
          >
            <span className="label">X {formatX(hover.x)}</span>
            {hoverReadout.map((item) => (
              <span key={item.key} className="channel-strip__tooltip-value" style={{ color: item.color }}>
                {item.label}: {tickFormat(item.point.y)}
              </span>
            ))}
          </div>
        )}

        {hover && (
          <span
            className="channel-strip__crosshair"
            aria-hidden="true"
            style={{ left: `${hover.ratio * 100}%`, top: `${activeHoverY}px` }}
          />
        )}

        {thresholds.map((t) => {
          const y = yToPixel(t.value, domain, height);
          const bandColor =
            t.kind === "danger" ? "var(--color-threshold-danger)" : "var(--color-threshold-warn)";
          return (
            <span
              key={t.label}
              className="channel-strip__threshold-tab label"
              style={{ top: Math.max(0, Math.min(y, height - 16)), color: bandColor, borderColor: bandColor }}
            >
              {t.label}
            </span>
          );
        })}

        {series.length > 0 &&
          [domain.yMax, (domain.yMax + domain.yMin) / 2, domain.yMin].map((value, i) => (
            <span
              key={i}
              className="channel-strip__tick num"
              style={{
                top: (height / 2) * i,
                transform: `translateY(${i === 0 ? "0%" : i === 2 ? "-100%" : "-50%"})`,
              }}
            >
              {tickFormat(value)}
            </span>
          ))}
      </div>
    </section>
  );
}

export default memo(ChannelStrip);
