"""Live RSS + IBKR news orchestration and local-model processing."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import queue
import threading
import time

from quant.news.catalog import load_rss_catalog
from quant.news.core import NewsAnalyzer, NewsArticle, NewsStore
from quant.news.ibkr import IbkrNewsClient
from quant.news.rss import RssPoller


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsServiceConfig:
    db_path: str = "quant/data/news.sqlite3"
    tickers: tuple[str, ...] = ()
    asset_class: str = "equity"
    rss_enabled: bool = True
    rss_catalog_path: str = ""
    rss_poll_seconds: int = 120
    ibkr_enabled: bool = True
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 30
    ibkr_broad_tape: bool = True
    ibkr_contract_specific: bool = True
    ibkr_provider_allowlist: tuple[str, ...] = ()
    ollama_url: str = "http://127.0.0.1:11434/api/generate"
    ollama_model: str = "lfm2:24b"
    ollama_timeout_seconds: float = 20.0


class NewsProcessor:
    """Persist a fast fallback immediately, then asynchronously enrich via LLM."""

    def __init__(self, store: NewsStore, analyzer: NewsAnalyzer) -> None:
        self.store = store
        self.analyzer = analyzer
        self._queue: queue.Queue[NewsArticle] = queue.Queue(maxsize=4000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._suspended_until = 0.0
        self._lock = threading.Lock()
        self._stats = {"ingested": 0, "duplicates": 0, "llm_enriched": 0, "llm_errors": 0}

    def start(self) -> None:
        if not self.analyzer.ollama_model:
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="news-analyzer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def ingest(self, article: NewsArticle) -> None:
        if not article.title.strip():
            return
        prior = self.store.article_state(article.article_id)
        deterministic = self.analyzer.deterministic(article)
        replace = prior is None or not str(prior.get("analysis_mode", "")).startswith("ollama:")
        inserted = self.store.put(article, deterministic, replace_analysis=replace)
        body_grew = prior is not None and len(article.body) > int(prior.get("body_length") or 0)
        with self._lock:
            self._stats["ingested" if inserted else "duplicates"] += 1
        if self.analyzer.ollama_model and (inserted or body_grew):
            try:
                self._queue.put_nowait(article)
            except queue.Full:
                LOG.warning("News analysis queue full; deterministic score retained")

    def status(self) -> dict:
        with self._lock:
            return {**self._stats, "queued": self._queue.qsize()}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                article = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                delay = self._suspended_until - time.monotonic()
                if delay > 0:
                    # Keep deterministic features current without allowing an
                    # unavailable Ollama daemon to block feed ingestion.
                    self._stop.wait(min(delay, 5.0))
                    try:
                        self._queue.put_nowait(article)
                    except queue.Full:
                        pass
                    continue
                analysis = self.analyzer.analyze(article, use_llm=True)
                self.store.put(article, analysis, replace_analysis=True)
                self._failures = 0
                with self._lock:
                    self._stats["llm_enriched"] += 1
            except Exception as exc:  # noqa: BLE001 - deterministic result remains usable
                self._failures += 1
                with self._lock:
                    self._stats["llm_errors"] += 1
                if self._failures >= 3:
                    self._suspended_until = time.monotonic() + 300.0
                    self._failures = 0
                LOG.warning("Local news model failed; deterministic score retained: %s", exc)
            finally:
                self._queue.task_done()


class NewsService:
    def __init__(self, config: NewsServiceConfig) -> None:
        self.config = config
        self.store = NewsStore(config.db_path)
        self.analyzer = NewsAnalyzer(
            config.tickers,
            ollama_url=config.ollama_url,
            ollama_model=config.ollama_model,
            timeout_seconds=config.ollama_timeout_seconds,
        )
        self.processor = NewsProcessor(self.store, self.analyzer)
        self.rss = (
            RssPoller(
                load_rss_catalog(config.rss_catalog_path or None),
                self.processor.ingest,
                poll_seconds=config.rss_poll_seconds,
            )
            if config.rss_enabled
            else None
        )
        self.ibkr = (
            IbkrNewsClient(
                self.processor.ingest,
                tickers=config.tickers,
                asset_class=config.asset_class,
                host=config.ibkr_host,
                port=config.ibkr_port,
                client_id=config.ibkr_client_id,
                broad_tape=config.ibkr_broad_tape,
                contract_specific=config.ibkr_contract_specific,
                provider_allowlist=config.ibkr_provider_allowlist,
                on_symbol_context=lambda symbol, industries, commodities: self.store.put_symbol_context(
                    symbol,
                    industries,
                    commodities,
                    source="ibkr_contract_details",
                ),
            )
            if config.ibkr_enabled
            else None
        )

    def start(self) -> None:
        self.processor.start()
        if self.rss:
            self.rss.start()
        if self.ibkr:
            self.ibkr.start()
        LOG.info(
            "News service active: db=%s rss=%s ibkr=%s tickers=%s",
            self.config.db_path,
            bool(self.rss),
            bool(self.ibkr),
            ",".join(self.config.tickers),
        )

    def stop(self) -> None:
        if self.ibkr:
            self.ibkr.stop()
        if self.rss:
            self.rss.stop()
        self.processor.stop()
        self.store.close()

    def status(self) -> dict:
        return {
            "database": self.config.db_path,
            "stored_articles": self.store.count(),
            "processor": self.processor.status(),
            "rss": self.rss.status() if self.rss else {"enabled": False},
            "ibkr": self.ibkr.status() if self.ibkr else {"enabled": False},
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture and score RSS + IBKR live news")
    parser.add_argument("--tickers", nargs="*", default=[])
    parser.add_argument("--asset-class", choices=("crypto", "equity"), default="equity")
    parser.add_argument("--db", default="quant/data/news.sqlite3")
    parser.add_argument("--rss-catalog", default="")
    parser.add_argument("--rss-poll-seconds", type=int, default=120)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=30)
    parser.add_argument("--no-ibkr", action="store_true")
    parser.add_argument("--no-rss", action="store_true")
    parser.add_argument("--no-broad-tape", action="store_true")
    parser.add_argument("--no-contract-news", action="store_true")
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/generate")
    parser.add_argument("--ollama-model", default="lfm2:24b")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    service = NewsService(
        NewsServiceConfig(
            db_path=args.db,
            tickers=tuple(args.tickers),
            asset_class=args.asset_class,
            rss_enabled=not args.no_rss,
            rss_catalog_path=args.rss_catalog,
            rss_poll_seconds=args.rss_poll_seconds,
            ibkr_enabled=not args.no_ibkr,
            ibkr_host=args.host,
            ibkr_port=args.port,
            ibkr_client_id=args.client_id,
            ibkr_broad_tape=not args.no_broad_tape,
            ibkr_contract_specific=not args.no_contract_news,
            ibkr_provider_allowlist=tuple(args.provider),
            ollama_url=args.ollama_url,
            ollama_model=args.ollama_model,
        )
    )
    service.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()


if __name__ == "__main__":
    main()
