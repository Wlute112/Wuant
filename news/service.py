"""Live RSS + IBKR news orchestration and local-model processing."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import threading
import time

from quant.news.catalog import load_rss_catalog
from quant.news.core import NewsAnalyzer, NewsArticle, NewsStore
from quant.news.ibkr import IbkrNewsClient
from quant.news.rss import RssPoller
from quant.ops.state import OperationsStore


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
    operations_db_path: str = ""
    operations_component_id: str = ""
    heartbeat_seconds: float = 5.0


class NewsProcessor:
    """Persist a fast fallback immediately, then asynchronously enrich via LLM."""

    def __init__(self, store: NewsStore, analyzer: NewsAnalyzer) -> None:
        self.store = store
        self.analyzer = analyzer
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
        recovery = self.store.recover_enrichment_queue()
        if recovery["released_claims"] or recovery["restored_articles"]:
            LOG.info(
                "Recovered news enrichment queue: released=%d restored=%d",
                recovery["released_claims"],
                recovery["restored_articles"],
            )
        self._stop.clear()
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
            self.store.enqueue_enrichment(article)

    def status(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "queued": self.store.pending_enrichment_count(),
                "running": bool(self._thread and self._thread.is_alive()),
                "llm_degraded": self._suspended_until > time.monotonic(),
            }

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                delay = self._suspended_until - time.monotonic()
                if delay > 0:
                    # Keep deterministic features current without allowing an
                    # unavailable Ollama daemon to block feed ingestion.
                    self._stop.wait(min(delay, 5.0))
                    continue
                item = self.store.claim_next_enrichment()
                if item is None:
                    self._stop.wait(1.0)
                    continue
                article, generation, attempts = item
                try:
                    analysis = self.analyzer.analyze(article, use_llm=True)
                    self.store.put(article, analysis, replace_analysis=True)
                    self.store.complete_enrichment(article.article_id, generation)
                    self._failures = 0
                    with self._lock:
                        self._stats["llm_enriched"] += 1
                except Exception as exc:  # noqa: BLE001 - deterministic result remains usable
                    self._failures += 1
                    with self._lock:
                        self._stats["llm_errors"] += 1
                    retry_delay = min(5.0 * (2 ** min(attempts - 1, 6)), 300.0)
                    if self._failures >= 3:
                        self._suspended_until = time.monotonic() + 300.0
                        retry_delay = max(retry_delay, 300.0)
                        self._failures = 0
                    self.store.retry_enrichment(
                        article.article_id,
                        generation,
                        f"{type(exc).__name__}: {exc}",
                        delay_seconds=retry_delay,
                    )
                    LOG.warning("Local news model failed; deterministic score retained: %s", exc)
        finally:
            # NewsStore connections are thread-local; close the worker's
            # connection without touching the service thread's connection.
            self.store.close()


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
        self._operations = (
            OperationsStore(config.operations_db_path)
            if config.operations_db_path and config.operations_component_id
            else None
        )
        self._operations_stop = threading.Event()
        self._operations_thread: threading.Thread | None = None
        self._last_operations_status = ""

    def start(self) -> None:
        self.processor.start()
        if self.rss:
            self.rss.start()
        if self.ibkr:
            self.ibkr.start()
        if self._operations is not None:
            self._operations_stop.clear()
            self._operations.append_event(
                self.config.operations_component_id,
                "NEWS_SERVICE_STARTED",
                {"database": self.config.db_path},
            )
            self._operations_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="news-operations-heartbeat",
                daemon=True,
            )
            self._operations_thread.start()
        LOG.info(
            "News service active: db=%s rss=%s ibkr=%s tickers=%s",
            self.config.db_path,
            bool(self.rss),
            bool(self.ibkr),
            ",".join(self.config.tickers),
        )

    def stop(self) -> None:
        self._operations_stop.set()
        if self._operations_thread and self._operations_thread.is_alive():
            self._operations_thread.join(timeout=max(self.config.heartbeat_seconds * 2, 2.0))
        if self.ibkr:
            self.ibkr.stop()
        if self.rss:
            self.rss.stop()
        self.processor.stop()
        final_status = self.status()
        self.store.close()
        if self._operations is not None:
            self._operations.heartbeat(
                self.config.operations_component_id,
                self.config.operations_component_id,
                status="STOPPED",
                details=final_status,
            )
            self._operations.append_event(
                self.config.operations_component_id,
                "NEWS_SERVICE_STOPPED",
                {},
            )
            self._operations.close()

    def _heartbeat_loop(self) -> None:
        while not self._operations_stop.is_set():
            status = self.status()
            source_states = []
            if self.rss is not None:
                feed_count = int(status["rss"].get("feed_count", 0))
                failed_feeds = len(status["rss"].get("errors") or {})
                source_states.append(
                    bool(status["rss"].get("running"))
                    and feed_count > 0
                    and failed_feeds < feed_count
                )
            if self.ibkr is not None:
                source_states.append(
                    bool(status["ibkr"].get("connected"))
                    and bool(
                        status["ibkr"].get("providers")
                        or status["ibkr"].get("subscriptions")
                    )
                )
            processor_healthy = (
                not self.analyzer.ollama_model
                or bool(status["processor"].get("running"))
            )
            queued = int(status["processor"].get("queued", 0))
            enrichment_degraded = bool(status["processor"].get("llm_degraded"))
            all_sources = all(source_states) if source_states else False
            any_source = any(source_states)
            operational_status = (
                "HEALTHY"
                if all_sources and processor_healthy and queued < 1000 and not enrichment_degraded
                else "DEGRADED"
                if any_source and processor_healthy and queued < 1000
                else "FAILED"
            )
            if operational_status != self._last_operations_status:
                self._operations.append_event(
                    self.config.operations_component_id,
                    "NEWS_SERVICE_HEALTH_CHANGED",
                    {"previous": self._last_operations_status, "current": operational_status},
                    severity=("INFO" if operational_status == "HEALTHY" else "WARNING" if operational_status == "DEGRADED" else "CRITICAL"),
                )
                self._last_operations_status = operational_status
            self._operations.heartbeat(
                self.config.operations_component_id,
                self.config.operations_component_id,
                status=operational_status,
                details=status,
            )
            self._operations_stop.wait(max(self.config.heartbeat_seconds, 1.0))

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
