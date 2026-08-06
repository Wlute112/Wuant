const VIEW_WIDTH = 1000;

export function computeDomain(seriesList) {
  let xMin = Infinity;
  let xMax = -Infinity;
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const series of seriesList) {
    for (const p of series) {
      if (p.x < xMin) xMin = p.x;
      if (p.x > xMax) xMax = p.x;
      if (p.y < yMin) yMin = p.y;
      if (p.y > yMax) yMax = p.y;
    }
  }
  if (!Number.isFinite(xMin)) {
    return { xMin: 0, xMax: 1, yMin: 0, yMax: 1 };
  }
  if (xMin === xMax) xMax = xMin + 1;
  if (yMin === yMax) {
    yMin -= 1;
    yMax += 1;
  }
  return { xMin, xMax, yMin, yMax };
}

/** Pad the y domain by a fraction of its span so a trace never touches the
 * strip's top/bottom edge. */
export function padDomain(domain, fraction = 0.12) {
  const span = domain.yMax - domain.yMin || 1;
  return {
    ...domain,
    yMin: domain.yMin - span * fraction,
    yMax: domain.yMax + span * fraction,
  };
}

export function buildPathD(points, domain, height) {
  if (!points || points.length === 0) return "";
  const { xMin, xMax, yMin, yMax } = domain;
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;
  const coords = points.map((p) => {
    const px = ((p.x - xMin) / xSpan) * VIEW_WIDTH;
    const py = height - ((p.y - yMin) / ySpan) * height;
    return `${px.toFixed(2)},${py.toFixed(2)}`;
  });
  return `M${coords.join(" L")}`;
}

export function yToPixel(value, domain, height) {
  const { yMin, yMax } = domain;
  const ySpan = yMax - yMin || 1;
  return height - ((value - yMin) / ySpan) * height;
}

export const TRACE_VIEW_WIDTH = VIEW_WIDTH;
