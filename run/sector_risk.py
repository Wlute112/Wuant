"""Reviewed, conId-bound sector evidence and gross look-through exposure.

Research symbol maps are deliberately not an execution authority. The operator
supplies dated issuer/classification-provider evidence; this module verifies its
contract and coverage, not the truth of the referenced external document.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from urllib.parse import urlparse

from quant.data.instrument_identity import EquityIdentity

SECTORS = frozenset({
    "communication_services", "consumer_discretionary", "consumer_staples",
    "energy", "financials", "healthcare", "industrials", "materials",
    "real_estate", "technology", "utilities",
})
MAX_EVIDENCE_DAYS = 31


def _instant(value) -> datetime:
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("timezone required")
        return instant.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("Sector evidence timestamps must include a timezone") from exc


def validate_sector_evidence(payload, *, symbols=None, now=None) -> dict:
    """Validate a complete, time-bounded operator-reviewed source snapshot."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("Sector evidence requires schema_version 1")
    if set(payload) != {"schema_version", "classifications"}:
        raise ValueError("Sector evidence accepts only schema_version and classifications")
    rows = payload.get("classifications")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError("Sector evidence requires 1–500 classifications")
    seen_ids, seen_symbols = set(), set()
    required = {"con_id", "symbol", "weights", "source_name", "source_url", "as_of",
                "reviewed_at", "reviewed_by", "valid_until"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(f"Each sector classification requires exactly: {', '.join(sorted(required))}")
        con_id, symbol = row["con_id"], row["symbol"]
        if type(con_id) is not int or con_id <= 0 or con_id in seen_ids:
            raise ValueError("Sector evidence con_id must be a unique positive broker conId")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper() or symbol in seen_symbols:
            raise ValueError("Sector evidence symbols must be unique uppercase symbols")
        seen_ids.add(con_id)
        seen_symbols.add(symbol)
        for field in ("source_name", "source_url", "reviewed_by"):
            value = row[field]
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2048 or any(ord(c) < 32 for c in value):
                raise ValueError(f"Sector evidence {field} is required and must be valid text")
        url = urlparse(row["source_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Sector evidence source_url must be an HTTPS source reference without credentials")
        as_of, reviewed, until = (_instant(row[key]) for key in ("as_of", "reviewed_at", "valid_until"))
        if not as_of <= reviewed <= now < until or (until - as_of).total_seconds() > MAX_EVIDENCE_DAYS * 86400:
            raise ValueError("Sector evidence is expired, future-dated, or exceeds the 31-day validity window")
        weights = row["weights"]
        if not isinstance(weights, dict) or not weights or not set(weights) <= SECTORS:
            raise ValueError("Sector weights must use supported sector names")
        if any(type(w) not in (int, float) or not math.isfinite(w) or not 0 < w <= 1 for w in weights.values()):
            raise ValueError("Sector weights must be finite fractions greater than zero and at most one")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6, rel_tol=0):
            raise ValueError("Sector weights must cover 100% of gross exposure; partial ETF holdings are not sufficient")
    missing = set(symbols or ()) - seen_symbols
    if missing:
        raise ValueError(f"Sector evidence is missing: {', '.join(sorted(missing))}")
    # JSON round-trip isolates caller mutations and rejects unsupported data.
    return json.loads(json.dumps(payload, allow_nan=False))


def sector_snapshot(payload, infos: dict, notionals: dict, *, equity: float, limit: float, now=None) -> dict:
    result = dict(healthy=False, status="UNAVAILABLE", basis="Gross USD exposure · reviewed sector weights",
                  sectors={}, classifications=[], issues=[], limit_pct=limit * 100,
                  evidence_sha256=None)
    try:
        evidence = validate_sector_evidence(payload, now=now)
        if not math.isfinite(equity) or equity <= 0 or not math.isfinite(limit) or not 0 < limit <= 1:
            raise ValueError("Sector risk requires positive finite equity and a sector limit in (0, 1]")
        result["evidence_sha256"] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        by_id = {row["con_id"]: row for row in evidence["classifications"]}
        for iid in sorted(set(infos) | set(notionals)):
            identity = EquityIdentity.from_info(infos.get(iid) or {})
            row = by_id.get(identity.con_id)
            if row is None or row["symbol"] != identity.symbol:
                raise ValueError(f"{iid}: sector evidence does not match qualified broker conId/symbol")
            amount = notionals.get(iid, 0.0)
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                raise ValueError(f"{iid}: gross exposure cannot be valued from current broker marks")
            result["classifications"].append({**row, "instrument_id": iid, "gross_notional": amount})
            for sector, weight in row["weights"].items():
                result["sectors"][sector] = result["sectors"].get(sector, 0.0) + amount * weight
        result["sectors"] = {
            name: dict(notional=amount, equity_pct=amount / equity * 100, breached=amount > equity * limit + 1e-9)
            for name, amount in sorted(result["sectors"].items())
        }
        breached = [name for name, values in result["sectors"].items() if values["breached"]]
        result.update(healthy=not breached, status="BREACHED" if breached else "CURRENT",
                      issues=[f"Sector exposure exceeds the configured limit: {', '.join(breached)}"] if breached else [])
    except (ValueError, TypeError, KeyError) as exc:
        result.update(status="UNAVAILABLE", sectors={}, issues=[str(exc)])
    return result
