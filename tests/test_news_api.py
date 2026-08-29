from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from quant.api import news_routes
from quant.news.core import NewsAnalysis, NewsArticle, NewsStore


def test_live_news_exposes_llm_connections_and_scaled_move(monkeypatch, tmp_path):
    path = tmp_path / "news.sqlite3"
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", path)
    store = NewsStore(str(path))
    store.put_symbol_context(
        "ACME", ("semiconductors",), source="ibkr_contract_details"
    )
    now = datetime.now(timezone.utc)
    store.put(
        NewsArticle(
            source_kind="ibkr",
            source_name="Briefing.com",
            provider="BRFG",
            external_id="story-1",
            title="Chip production expansion approved",
            url="javascript:alert(1)",
            published_at=now - timedelta(minutes=2),
            received_at=now - timedelta(minutes=1),
            metadata={"ibkr_scope": "broad", "source_weight": 1.0},
        ),
        NewsAnalysis(
            summary="Capacity is expected to rise.",
            symbol_scores={"SPY": -0.4},
            industry_scores={"semiconductors": 0.8},
            commodity_scores={"copper": 0.5},
            macro_score=0.1,
            urgency=7,
            confidence=0.82,
            mode="ollama:lfm2:24b",
        ),
    )
    store.close()

    payload = news_routes.get_live_news(
        tickers="ACME,SPY", limit=20, news_raw_scale=0.001
    )

    assert payload["status"] == "available"
    assert payload["prediction_basis"] == "analysis_scenario_x_news_raw_scale"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["analysis_kind"] == "local_llm"
    assert item["source_name"] == "Briefing.com"
    assert item["url"] == ""
    assert item["commodities"] == [{"name": "copper", "score": 0.5}]
    impacts = {impact["symbol"]: impact for impact in item["connections"]}
    assert impacts["ACME"]["direction"] == "UP"
    assert impacts["ACME"]["predicted_move_pct"] == pytest.approx(
        impacts["ACME"]["score"] * 0.1,
        abs=1e-6,
    )
    assert "industry:semiconductors" in impacts["ACME"]["drivers"]
    assert item["connected_to_strategy"] is True

    causal = news_routes.get_live_news(
        tickers="ACME,SPY",
        limit=20,
        news_raw_scale=0.001,
        factor_enabled=True,
        factor_as_of=now + timedelta(seconds=1),
    )
    assert causal["prediction_basis"] == "causal_marginal_factor_contribution"
    causal_item = causal["items"][0]
    assert causal_item["factor_eligible"] is True
    causal_impacts = {impact["symbol"]: impact for impact in causal_item["connections"]}
    assert causal_impacts["ACME"]["factor_eligible"] is True
    assert causal_impacts["ACME"]["effective_reliability"] > 0
    assert causal_impacts["ACME"]["predicted_move_pct"] != impacts["ACME"]["predicted_move_pct"]


def test_live_news_fit_mode_does_not_invent_article_move(monkeypatch, tmp_path):
    path = tmp_path / "news.sqlite3"
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", path)
    store = NewsStore(str(path))
    store.put(
        NewsArticle(
            source_kind="rss",
            source_name="Test wire",
            external_id="fit-story",
            title="Index outlook changes",
            published_at=datetime.now(timezone.utc),
        ),
        NewsAnalysis(symbol_scores={"SPY": 0.5}, confidence=0.8),
    )
    store.close()

    payload = news_routes.get_live_news(
        tickers="SPY",
        limit=20,
        news_raw_scale=0.001,
        news_source="fit",
        factor_enabled=True,
    )

    assert payload["prediction_basis"] == "fit_coefficient_not_available_per_article"
    connection = payload["items"][0]["connections"][0]
    assert connection["direction"] is None
    assert connection["analysis_direction"] == "UP"
    assert connection["predicted_move_pct"] is None


def test_live_news_missing_database_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", tmp_path / "missing.sqlite3")
    payload = news_routes.get_live_news(
        tickers="SPY", limit=20, news_raw_scale=0.001
    )
    assert payload["status"] == "unavailable"
    assert payload["items"] == []
    assert payload["latest_received_at"] is None


def test_live_news_uses_database_from_trusted_job_telemetry(monkeypatch, tmp_path):
    custom = tmp_path / "custom-news.sqlite3"
    store = NewsStore(str(custom))
    store.put(
        NewsArticle(
            source_kind="rss",
            source_name="Custom wire",
            external_id="custom-story",
            title="Custom archive headline",
            published_at=datetime.now(timezone.utc),
        ),
        NewsAnalysis(symbol_scores={"SPY": 0.3}),
    )
    store.close()
    monkeypatch.setattr(news_routes, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", tmp_path / "missing.sqlite3")
    job_id = "paper_0123456789"
    (tmp_path / f"{job_id}_telemetry.json").write_text(
        json.dumps({"model": {"news_data_path": str(custom)}})
    )

    payload = news_routes.get_live_news(tickers="SPY", job_id=job_id)

    assert payload["status"] == "available"
    assert payload["database_source"] == "active_job"
    assert payload["database_name"] == custom.name
    assert payload["items"][0]["title"] == "Custom archive headline"


def test_live_news_proportionally_applies_score_clip(monkeypatch, tmp_path):
    path = tmp_path / "news.sqlite3"
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", path)
    store = NewsStore(str(path))
    now = datetime.now(timezone.utc)
    for index in range(2):
        store.put(
            NewsArticle(
                source_kind="rss",
                source_name="Test wire",
                external_id=f"clip-{index}",
                title=f"Positive event {index}",
                published_at=now - timedelta(minutes=index + 1),
                received_at=now - timedelta(minutes=index + 1),
            ),
            NewsAnalysis(
                symbol_scores={"SPY": 1.0}, urgency=10, confidence=1.0
            ),
        )
    store.close()

    payload = news_routes.get_live_news(
        tickers="SPY",
        factor_enabled=True,
        factor_as_of=now + timedelta(seconds=1),
        news_raw_scale=0.001,
        news_score_clip=0.2,
    )

    moves = [
        item["connections"][0]["predicted_move_pct"]
        for item in payload["items"]
    ]
    assert sum(moves) == pytest.approx(0.02, abs=1e-6)
    assert payload["max_impulse_pct"] == pytest.approx(0.02)


def test_live_news_distinguishes_latest_and_causal_analysis(monkeypatch, tmp_path):
    path = tmp_path / "news.sqlite3"
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", path)
    store = NewsStore(str(path))
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        source_kind="rss",
        source_name="Test wire",
        external_id="refined-story",
        title="Analysis is refined",
        published_at=now - timedelta(minutes=2),
        received_at=now - timedelta(minutes=1),
    )
    store.put(
        article,
        NewsAnalysis(
            symbol_scores={"SPY": -0.3},
            urgency=8,
            confidence=0.8,
            mode="deterministic",
        ),
    )
    factor_as_of = datetime.fromtimestamp(time.time(), timezone.utc)
    time.sleep(0.01)
    store.put(
        article,
        NewsAnalysis(
            symbol_scores={"SPY": 0.7},
            urgency=8,
            confidence=0.8,
            mode="ollama:lfm2:24b",
        ),
    )
    store.close()

    payload = news_routes.get_live_news(
        tickers="SPY", factor_enabled=True, factor_as_of=factor_as_of
    )

    item = payload["items"][0]
    assert item["analysis_kind"] == "local_llm"
    assert item["factor_analysis_kind"] == "deterministic_fallback"
    assert item["analysis_superseded"] is True
    assert item["connections"][0]["direction"] == "DOWN"
    assert item["connections"][0]["analysis_direction"] == "UP"


def test_live_news_retains_causal_link_removed_by_latest_analysis(
    monkeypatch, tmp_path
):
    path = tmp_path / "news.sqlite3"
    monkeypatch.setattr(news_routes, "NEWS_DB_PATH", path)
    store = NewsStore(str(path))
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        source_kind="rss",
        source_name="Test wire",
        external_id="removed-link",
        title="Ticker link is removed",
        published_at=now - timedelta(minutes=2),
        received_at=now - timedelta(minutes=1),
    )
    store.put(
        article,
        NewsAnalysis(
            symbol_scores={"SPY": -0.4},
            urgency=8,
            confidence=0.8,
            mode="deterministic",
        ),
    )
    factor_as_of = datetime.fromtimestamp(time.time(), timezone.utc)
    time.sleep(0.01)
    store.put(
        article,
        NewsAnalysis(
            urgency=8,
            confidence=0.8,
            mode="ollama:lfm2:24b",
        ),
    )
    store.close()

    payload = news_routes.get_live_news(
        tickers="SPY", factor_enabled=True, factor_as_of=factor_as_of
    )

    item = payload["items"][0]
    connection = item["connections"][0]
    assert item["connection_superseded"] is True
    assert connection["symbol"] == "SPY"
    assert connection["latest_connected"] is False
    assert connection["direction"] == "DOWN"
    assert connection["analysis_direction"] is None
    assert connection["predicted_move_pct"] < 0
