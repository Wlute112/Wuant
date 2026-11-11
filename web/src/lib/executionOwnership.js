export function ownershipIsFresh(report, receivedAt, now = Date.now()) {
  const observed = Date.parse(report?.as_of);
  return Number.isFinite(observed) && Number.isFinite(receivedAt)
    && now >= receivedAt && now - receivedAt <= 10000
    && now - observed >= -5000 && now - observed <= 10000;
}
