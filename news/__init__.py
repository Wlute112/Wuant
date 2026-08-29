"""Causal live-news ingestion and alpha features.

The package normalizes RSS and IBKR news into one append-only store.  Trading
code reads only bounded numeric features whose ``received_at`` timestamp is not
later than the bar being evaluated.
"""

from quant.news.core import (
    NewsAnalyzer,
    NewsArticleContribution,
    NewsArticle,
    NewsFeatureReader,
    NewsFeatureSnapshot,
    NewsSymbolImpact,
    NewsStore,
    classify_industries,
    snapshot_news_store,
)

__all__ = [
    "NewsAnalyzer",
    "NewsArticleContribution",
    "NewsArticle",
    "NewsFeatureReader",
    "NewsFeatureSnapshot",
    "NewsSymbolImpact",
    "NewsStore",
    "classify_industries",
    "snapshot_news_store",
]
