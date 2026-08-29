from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import numpy as np
import pytest

from quant.models.prediction_engine import (
    PredictionConfig,
    PredictionEngine,
    make_features_targets,
)
from quant.news.core import (
    NewsAnalysis,
    NewsAnalyzer,
    NewsArticle,
    NewsFeatureReader,
    NewsStore,
    classify_industries,
    snapshot_news_store,
)


def _article(now: datetime, *, external_id: str = "event-1") -> NewsArticle:
    return NewsArticle(
        source_kind="rss",
        source_name="test",
        external_id=external_id,
        title="SPY raises guidance after record profit",
        published_at=now - timedelta(minutes=2),
        received_at=now - timedelta(minutes=1),
        symbols=("SPY",),
        metadata={"source_weight": 1.0},
    )


def test_news_store_and_reader_enforce_received_and_analysis_time(tmp_path):
    db = tmp_path / "news.sqlite3"
    now = datetime.now(timezone.utc)
    article = _article(now)
    store = NewsStore(str(db))
    store.put(
        article,
        NewsAnalysis(
            symbol_scores={"SPY": 0.9},
            urgency=10,
            confidence=1.0,
        ),
    )

    reader = NewsFeatureReader(str(db))
    try:
        assert reader.snapshot_at("SPY", now - timedelta(minutes=3)).score == 0.0
        # The event was received earlier, but its analysis did not exist until
        # put() completed. Historical replay cannot borrow that later result.
        assert reader.snapshot_at("SPY", time.time() - 1.0).score == 0.0
        available = reader.snapshot_at("SPY", time.time() + 1.0)
        assert available.score > 0.0
        assert available.article_count == 1
        contributions = reader.article_contributions_at("SPY", time.time() + 1.0)
        assert len(contributions) == 1
        assert sum(item.effective_score for item in contributions) == pytest.approx(
            available.score, rel=1e-5
        )
    finally:
        reader.close()
        store.close()


def test_dedup_preserves_first_received_timestamp(tmp_path):
    db = tmp_path / "news.sqlite3"
    now = datetime.now(timezone.utc)
    first = _article(now)
    later = NewsArticle(
        **{
            **first.__dict__,
            "received_at": now + timedelta(hours=1),
            "body": "Expanded article body",
        }
    )
    store = NewsStore(str(db))
    try:
        assert store.put(first, NewsAnalysis(symbol_scores={"SPY": 0.4})) is True
        assert store.put(later, NewsAnalysis(symbol_scores={"SPY": 0.6})) is False
        assert store.count() == 1
        row = store.latest(1)[0]
        assert row["received_at"] == pytest.approx(first.received_at.timestamp())
        assert row["body"] == "Expanded article body"
    finally:
        store.close()


def test_snapshot_is_immutable_after_source_store_changes(tmp_path):
    source = tmp_path / "live.sqlite3"
    now = datetime.now(timezone.utc)
    store = NewsStore(str(source))
    store.put(_article(now), NewsAnalysis(symbol_scores={"SPY": 0.5}))
    snapshot, digest = snapshot_news_store(str(source), str(tmp_path / "snapshots"))
    store.put(
        _article(now, external_id="event-2"),
        NewsAnalysis(symbol_scores={"SPY": -0.5}),
    )
    frozen = NewsStore(snapshot)
    try:
        assert len(digest) == 64
        assert frozen.count() == 1
        assert store.count() == 2
    finally:
        frozen.close()
        store.close()


def test_deterministic_analyzer_bounds_and_maps_market_context():
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        source_kind="rss",
        source_name="test",
        title="Copper mine shutdown creates supply disruption",
        summary="Unexpected outage hits industrial metals production",
        published_at=now,
    )
    result = NewsAnalyzer(("SPY",), ollama_model="").deterministic(article)
    assert result.commodity_scores["copper"] > 0.0
    assert "materials_mining" in result.industry_scores
    assert 1 <= result.urgency <= 10
    assert all(-1.0 <= score <= 1.0 for score in result.commodity_scores.values())


def test_broker_industry_context_reaches_non_etf_symbol(tmp_path):
    db = tmp_path / "news.sqlite3"
    now = datetime.now(timezone.utc)
    store = NewsStore(str(db))
    industries = classify_industries("Technology Semiconductors Computer Services")
    store.put_symbol_context("ACME", industries, source="ibkr_contract_details")
    store.put(
        NewsArticle(
            source_kind="rss",
            source_name="test",
            external_id="sector-event",
            title="Semiconductor production increase beats estimates",
            published_at=now - timedelta(minutes=2),
            received_at=now - timedelta(minutes=1),
            industries=("semiconductors",),
        ),
        NewsAnalysis(
            industry_scores={"semiconductors": 0.8},
            urgency=10,
            confidence=1.0,
        ),
    )
    reader = NewsFeatureReader(str(db))
    try:
        assert "semiconductors" in industries
        assert reader.snapshot_at("ACME", time.time() + 1.0).score > 0.0
    finally:
        reader.close()
        store.close()


def test_news_column_alignment_and_raw_prediction_contribution():
    rng = np.random.default_rng(11)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, 100)))
    news = np.linspace(-1.0, 1.0, len(closes))
    cfg = PredictionConfig(
        n_lags=3,
        horizon=1,
        min_train_bars=30,
        use_regime_features=False,
        use_news_features=True,
        news_source="raw",
        news_raw_scale=0.002,
    )
    X, _, idx = make_features_targets(closes, cfg, news_feats=news.reshape(-1, 1))
    assert X.shape[1] == cfg.n_lags + 1
    assert np.allclose(X[:, -1], news[idx])

    engine = PredictionEngine(cfg)
    zeros = np.zeros(len(closes))
    assert engine.refit_on_history(closes, news_features=zeros)
    neutral = engine.predict_move(closes, news_features=zeros)
    event = zeros.copy()
    event[-1] = 1.0
    with_event = engine.predict_move(closes, news_features=event)
    assert neutral is not None and with_event is not None
    assert with_event - neutral == pytest.approx(0.002)
