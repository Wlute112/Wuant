"""Read-only rolling-news API for the paper/live dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Query

from quant.api.jobs import JOBS_DIR
from quant.news.core import NewsFeatureReader
from quant.run.telemetry import load_telemetry


router = APIRouter(prefix="/api/live", tags=["live-news"])
NEWS_DB_PATH = Path(
    os.environ.get(
        "QUANT_NEWS_DB_PATH",
        Path(__file__).resolve().parents[1] / "data" / "news.sqlite3",
    )
)
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")
_JOB_ID = re.compile(r"^(paper|live)_[a-f0-9]{10}$")


def _iso_epoch(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _score_rows(values) -> list[dict]:
    if not isinstance(values, dict):
        return []
    rows = [
        {"name": str(name), "score": round(float(score), 6)}
        for name, score in values.items()
        if isinstance(score, (int, float)) and float(score) != 0.0
    ]
    return sorted(rows, key=lambda row: abs(row["score"]), reverse=True)


def _safe_article_url(value: object) -> str:
    url = str(value or "").strip()
    try:
        return url if urlsplit(url).scheme.lower() in {"http", "https"} else ""
    except ValueError:
        return ""


def _prediction_basis(*, factor_enabled: bool, news_source: str) -> str:
    if not factor_enabled:
        return "analysis_scenario_x_news_raw_scale"
    if news_source == "fit":
        return "fit_coefficient_not_available_per_article"
    return "causal_marginal_factor_contribution"


def _news_database(job_id: str | None) -> tuple[Path, str]:
    if job_id and _JOB_ID.fullmatch(job_id):
        telemetry = load_telemetry(JOBS_DIR / f"{job_id}_telemetry.json")
        configured = telemetry and telemetry.get("model", {}).get("news_data_path")
        if configured:
            return Path(str(configured)).expanduser(), "active_job"
    return Path(NEWS_DB_PATH), "default"


@router.get("/news")
def get_live_news(
    tickers: str = "",
    job_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 80,
    news_raw_scale: Annotated[float, Query(ge=0.0, le=1.0)] = 0.001,
    news_score_clip: Annotated[float, Query(gt=0.0, le=1.0)] = 1.0,
    news_source: Literal["raw", "fit"] = "raw",
    factor_enabled: bool = False,
    factor_as_of: datetime | None = None,
    half_life_hours: Annotated[float, Query(ge=0.25, le=720.0)] = 12.0,
    max_age_hours: Annotated[float, Query(ge=0.25, le=2160.0)] = 72.0,
    direct_weight: Annotated[float, Query(ge=0.0, le=10.0)] = 1.0,
    industry_weight: Annotated[float, Query(ge=0.0, le=10.0)] = 0.45,
    commodity_weight: Annotated[float, Query(ge=0.0, le=10.0)] = 0.55,
    macro_weight: Annotated[float, Query(ge=0.0, le=10.0)] = 0.20,
):
    """Latest normalized articles plus instrument-level LLM impact estimates.

    In an enabled raw-news configuration, ``predicted_move_pct`` is the
    article's causal marginal share of the aggregate news feature multiplied
    by the configured scale. Analysis-only and fitted modes are labeled
    separately. It is never total strategy ``yhat`` or a recommendation.
    """
    requested = tuple(
        dict.fromkeys(
            value
            for value in (part.strip().upper() for part in tickers.split(","))
            if _TICKER.fullmatch(value)
        )
    )[:100]
    now = datetime.now(timezone.utc)
    effective_as_of = factor_as_of or now
    if effective_as_of.tzinfo is None:
        effective_as_of = effective_as_of.replace(tzinfo=timezone.utc)
    basis = _prediction_basis(
        factor_enabled=bool(factor_enabled), news_source=news_source
    )
    path, database_source = _news_database(job_id)
    if not path.is_file():
        return {
            "status": "unavailable",
            "as_of": now.isoformat(),
            "factor_as_of": effective_as_of.isoformat(),
            "latest_received_at": None,
            "scale": float(news_raw_scale),
            "score_clip": float(news_score_clip),
            "max_impulse_pct": (
                float(news_raw_scale)
                * (float(news_score_clip) if factor_enabled else 1.0)
                * 100.0
            ),
            "prediction_basis": basis,
            "database_source": database_source,
            "database_name": path.name,
            "items": [],
        }

    reader = NewsFeatureReader(
        str(path),
        half_life_hours=half_life_hours,
        max_age_hours=max_age_hours,
        direct_weight=direct_weight,
        industry_weight=industry_weight,
        commodity_weight=commodity_weight,
        macro_weight=macro_weight,
        cache_symbol_context=True,
    )
    try:
        parsed_rows = []
        symbols = set(requested)
        for row in reader.store.latest(int(limit)):
            try:
                analysis = json.loads(row.get("analysis_json") or "{}")
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(analysis, dict):
                continue
            direct_symbols = {
                str(symbol).upper()
                for symbol in analysis.get("symbol_scores", {})
                if _TICKER.fullmatch(str(symbol).upper())
            }
            symbols.update(direct_symbols)
            parsed_rows.append((row, analysis, metadata, direct_symbols))

        contributions = {}
        contribution_scales = {}
        if factor_enabled:
            by_symbol = reader.article_contributions_for_symbols(
                symbols, effective_as_of
            )
            contributions = {
                symbol: {
                    item.article_id: item
                    for item in by_symbol.get(symbol, ())
                }
                for symbol in symbols
            }
            for symbol, symbol_contributions in by_symbol.items():
                total = sum(
                    item.effective_score for item in symbol_contributions
                )
                clipped = min(max(total, -float(news_score_clip)), float(news_score_clip))
                contribution_scales[symbol] = clipped / total if total else 1.0

        items = []
        for row, analysis, metadata, direct_symbols in parsed_rows:
            connections = []
            for symbol in dict.fromkeys((*requested, *sorted(direct_symbols))):
                impact = reader.impact_for_analysis(symbol, analysis)
                contribution = contributions.get(symbol, {}).get(row["article_id"])
                latest_connected = bool(
                    impact.drivers and abs(impact.score) >= 1e-12
                )
                if not latest_connected and contribution is None:
                    continue
                factor_eligible = contribution is not None
                drivers = contribution.drivers if contribution is not None else impact.drivers
                if not factor_enabled:
                    displayed_score = impact.score
                    direction = "UP" if impact.score > 0 else "DOWN"
                    predicted_move_pct = impact.score * float(news_raw_scale) * 100.0
                elif news_source == "raw" and contribution is not None:
                    displayed_score = (
                        contribution.effective_score
                        * contribution_scales.get(symbol, 1.0)
                    )
                    direction = "UP" if displayed_score > 0 else "DOWN"
                    predicted_move_pct = displayed_score * float(news_raw_scale) * 100.0
                else:
                    displayed_score = contribution.effective_score if contribution else impact.score
                    direction = None
                    predicted_move_pct = None
                connections.append(
                    {
                        "symbol": symbol,
                        "score": round(displayed_score, 6),
                        "analysis_score": round(impact.score, 6),
                        "factor_analysis_score": (
                            round(contribution.impact_score, 6)
                            if contribution is not None
                            else None
                        ),
                        "direction": direction,
                        "analysis_direction": (
                            "UP" if impact.score > 0 else "DOWN"
                            if impact.score < 0
                            else None
                        ),
                        "latest_connected": latest_connected,
                        "predicted_move_pct": (
                            round(predicted_move_pct, 6)
                            if predicted_move_pct is not None
                            else None
                        ),
                        "drivers": list(drivers),
                        "in_strategy": symbol in requested,
                        "factor_eligible": factor_eligible,
                        "effective_reliability": (
                            round(contribution.reliability, 6)
                            if contribution is not None
                            else None
                        ),
                        "analysis_mode_used": (
                            contribution.analysis_mode
                            if contribution is not None
                            else None
                        ),
                    }
                )
            connections.sort(
                key=lambda item: (not item["in_strategy"], -abs(item["score"]), item["symbol"])
            )

            mode = str(row.get("analysis_mode") or analysis.get("mode") or "deterministic")
            used_modes = {
                item["analysis_mode_used"]
                for item in connections
                if item["in_strategy"] and item["factor_eligible"]
            }
            factor_analysis_mode = sorted(used_modes)[0] if used_modes else None
            items.append(
                {
                    "id": row["article_id"],
                    "published_at": _iso_epoch(row["published_at"]),
                    "received_at": _iso_epoch(row["received_at"]),
                    "source_kind": row["source_kind"],
                    "source_name": row["source_name"],
                    "provider": row.get("provider") or "",
                    "title": row["title"],
                    "summary": str(analysis.get("summary") or row.get("summary") or "")[:500],
                    "url": _safe_article_url(row.get("url")),
                    "analysis_mode": mode,
                    "analysis_kind": "local_llm" if mode.startswith("ollama:") else "deterministic_fallback",
                    "factor_analysis_mode": factor_analysis_mode,
                    "factor_analysis_kind": (
                        "local_llm"
                        if factor_analysis_mode
                        and factor_analysis_mode.startswith("ollama:")
                        else "deterministic_fallback"
                        if factor_analysis_mode
                        else None
                    ),
                    "analysis_superseded": bool(
                        factor_analysis_mode and factor_analysis_mode != mode
                    ),
                    "connection_superseded": any(
                        item["in_strategy"]
                        and item["factor_eligible"]
                        and not item["latest_connected"]
                        for item in connections
                    ),
                    "confidence": round(float(analysis.get("confidence", 0.0)), 4),
                    "urgency": min(max(int(analysis.get("urgency", 1)), 1), 10),
                    "macro_score": round(float(analysis.get("macro_score", 0.0)), 6),
                    "industries": _score_rows(analysis.get("industry_scores")),
                    "commodities": _score_rows(analysis.get("commodity_scores")),
                    "connections": connections[:12],
                    "connected_to_strategy": any(
                        item["in_strategy"] for item in connections
                    ),
                    "factor_eligible": any(
                        item["in_strategy"] and item["factor_eligible"]
                        for item in connections
                    ),
                    "scope": metadata.get("ibkr_scope") or "broad",
                }
            )
        latest_received = items[0]["received_at"] if items else None
        return {
            "status": "available",
            "as_of": now.isoformat(),
            "factor_as_of": effective_as_of.isoformat(),
            "latest_received_at": latest_received,
            "scale": float(news_raw_scale),
            "score_clip": float(news_score_clip),
            "max_impulse_pct": (
                float(news_raw_scale)
                * (float(news_score_clip) if factor_enabled else 1.0)
                * 100.0
            ),
            "prediction_basis": basis,
            "database_source": database_source,
            "database_name": path.name,
            "items": items,
        }
    finally:
        reader.close()
