"""Read-only IBKR BroadTape and contract-specific live-news client."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import queue
import re
import threading
import time
from typing import Callable

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from quant.news.core import NewsArticle, classify_industries
from quant.news.rss import strip_html


LOG = logging.getLogger(__name__)
_PROVIDER_CODE = re.compile(r"^[A-Z0-9-]{1,20}$")
_INFORMATIONAL_CODES = {2104, 2106, 2107, 2108, 2158}


def _instrument_contract(symbol: str, asset_class: str) -> Contract:
    contract = Contract()
    contract.symbol = symbol.upper()
    contract.currency = "USD"
    if asset_class == "equity":
        contract.secType = "STK"
        contract.exchange = "SMART"
    elif asset_class == "crypto":
        contract.secType = "CRYPTO"
        contract.exchange = "ZEROHASH"
    else:
        raise ValueError("asset_class must be 'equity' or 'crypto'")
    return contract


def _broad_tape_contract(provider: str) -> Contract:
    contract = Contract()
    contract.symbol = f"{provider}:{provider}_ALL"
    contract.secType = "NEWS"
    contract.exchange = provider
    return contract


class _IbNewsClient(EWrapper, EClient):
    def __init__(self, owner: "IbkrNewsClient") -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.owner = owner
        self.ready = threading.Event()

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB API callback
        self.ready.set()

    def newsProviders(self, providers) -> None:  # noqa: N802 - IB API callback
        self.owner._receive_providers(providers)

    def tickNews(  # noqa: N802 - IB API callback
        self, tickerId, timeStamp, providerCode, articleId, headline, extraData
    ) -> None:
        self.owner._receive_headline(
            int(tickerId), int(timeStamp), str(providerCode), str(articleId),
            str(headline), str(extraData or ""),
        )

    def newsArticle(self, requestId, articleType, articleText) -> None:  # noqa: N802
        self.owner._receive_article(int(requestId), int(articleType), str(articleText or ""))

    def contractDetails(self, requestId, contractDetails) -> None:  # noqa: N802
        self.owner._receive_contract_details(contractDetails)

    def error(self, reqId, *args) -> None:
        if len(args) >= 4:
            _, code, message, _ = args[:4]
        elif len(args) >= 2:
            code, message = args[:2]
        else:
            return
        self.owner._receive_error(int(reqId), int(code), str(message))

    def connectionClosed(self) -> None:  # noqa: N802 - IB API callback
        self.owner._set_connection(False, "IBKR news socket closed")


class IbkrNewsClient:
    """Maintains read-only news subscriptions on a dedicated TWS client id.

    ``reqNewsProviders`` returns only sources already available to the account.
    The class never purchases or changes an account entitlement; it dynamically
    requests BroadTape and contract-specific streams for those returned codes.
    """

    def __init__(
        self,
        on_article: Callable[[NewsArticle], None],
        *,
        tickers: tuple[str, ...],
        asset_class: str,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 30,
        broad_tape: bool = True,
        contract_specific: bool = True,
        provider_allowlist: tuple[str, ...] = (),
        on_symbol_context: Callable[[str, tuple[str, ...], tuple[str, ...]], None] | None = None,
    ) -> None:
        self.on_article = on_article
        self.tickers = tuple(sorted({str(v).upper() for v in tickers if str(v).strip()}))
        self.asset_class = asset_class
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.broad_tape = bool(broad_tape)
        self.contract_specific = bool(contract_specific)
        self.provider_allowlist = tuple(str(v).upper() for v in provider_allowlist)
        self.on_symbol_context = on_symbol_context
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._article_thread: threading.Thread | None = None
        self._client: _IbNewsClient | None = None
        self._lock = threading.RLock()
        self._next_request_id = 900_000
        self._subscription_scopes: dict[int, str] = {}
        self._article_pending: dict[int, NewsArticle] = {}
        self._article_requests: queue.Queue[tuple[NewsArticle, str, str]] = queue.Queue(maxsize=2000)
        self._providers: dict[str, str] = {}
        self._state = {
            "connected": False,
            "message": "not started",
            "last_error": None,
            "headlines": 0,
            "articles": 0,
            "symbol_contexts": 0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="news-ibkr", daemon=True)
        self._article_thread = threading.Thread(
            target=self._request_articles, name="news-ibkr-articles", daemon=True
        )
        self._thread.start()
        self._article_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._disconnect()
        for thread in (self._thread, self._article_thread):
            if thread and thread.is_alive():
                thread.join(timeout=5)

    def status(self) -> dict:
        with self._lock:
            return {
                **self._state,
                "providers": dict(self._providers),
                "subscriptions": len(self._subscription_scopes),
                "client_id": self.client_id,
            }

    def _request_id(self) -> int:
        with self._lock:
            value = self._next_request_id
            self._next_request_id += 1
            return value

    def _run(self) -> None:
        while not self._stop.is_set():
            client = _IbNewsClient(self)
            self._client = client
            try:
                client.connect(self.host, self.port, self.client_id)
                reader = threading.Thread(target=client.run, name="news-ibkr-reader", daemon=True)
                reader.start()
                if not client.ready.wait(timeout=12):
                    raise TimeoutError("IBKR news handshake timed out")
                self._set_connection(True, "IBKR news connected; requesting providers")
                client.reqNewsProviders()
                if self.on_symbol_context is not None:
                    for symbol in self.tickers:
                        client.reqContractDetails(
                            self._request_id(),
                            _instrument_contract(symbol, self.asset_class),
                        )
                while not self._stop.wait(2.0) and client.isConnected():
                    pass
            except Exception as exc:  # noqa: BLE001 - reconnect while RSS remains available
                self._set_connection(False, str(exc))
                LOG.warning("IBKR news connection failed: %s", exc)
            finally:
                self._disconnect()
            self._stop.wait(5.0)

    def _disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        with self._lock:
            request_ids = list(self._subscription_scopes)
            self._subscription_scopes.clear()
        for request_id in request_ids:
            try:
                client.cancelMktData(request_id)
            except Exception:  # noqa: BLE001 - cleanup continues
                pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - cleanup continues
            pass
        if self._client is client:
            self._client = None

    def _set_connection(self, connected: bool, message: str) -> None:
        with self._lock:
            self._state["connected"] = connected
            self._state["message"] = message

    def _receive_providers(self, providers) -> None:
        available = {
            str(item.providerCode).upper(): str(item.providerName)
            for item in providers
            if _PROVIDER_CODE.fullmatch(str(item.providerCode).upper())
        }
        if self.provider_allowlist:
            wanted = set(self.provider_allowlist)
            available = {code: name for code, name in available.items() if code in wanted}
        with self._lock:
            self._providers = available
            self._state["message"] = (
                f"IBKR news providers: {', '.join(sorted(available))}"
                if available
                else "IBKR returned no API news providers; RSS remains active"
            )
        if available:
            LOG.info("IBKR API news providers available: %s", ", ".join(sorted(available)))
        else:
            LOG.warning("IBKR returned no API news providers; RSS remains active")
        client = self._client
        if not client or not client.isConnected() or not available:
            return
        codes = tuple(sorted(available))
        if self.broad_tape:
            for code in codes:
                request_id = self._request_id()
                with self._lock:
                    self._subscription_scopes[request_id] = "broad"
                client.reqMktData(
                    request_id, _broad_tape_contract(code), "mdoff,292", False, False, []
                )
        if self.contract_specific:
            generic_ticks = f"mdoff,292:{'+'.join(codes)}"
            for symbol in self.tickers:
                request_id = self._request_id()
                with self._lock:
                    self._subscription_scopes[request_id] = symbol
                client.reqMktData(
                    request_id,
                    _instrument_contract(symbol, self.asset_class),
                    generic_ticks,
                    False,
                    False,
                    [],
                )

    def _receive_headline(
        self,
        request_id: int,
        published_epoch: int,
        provider: str,
        article_id: str,
        headline: str,
        extra_data: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        # TWS versions have emitted this field in both Unix seconds and Unix
        # milliseconds. Normalize defensively before constructing the event.
        if published_epoch > 10_000_000_000:
            published_epoch //= 1000
        try:
            published = datetime.fromtimestamp(published_epoch, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            published = now
        if published > now:
            published = now
        with self._lock:
            scope = self._subscription_scopes.get(request_id, "broad")
            provider_name = self._providers.get(provider, provider)
            self._state["headlines"] += 1
        symbols = (scope,) if scope != "broad" else ()
        article = NewsArticle(
            source_kind="ibkr",
            source_name=provider_name,
            provider=provider,
            external_id=article_id,
            title=strip_html(headline, 2000),
            published_at=published,
            received_at=now,
            symbols=symbols,
            metadata={
                "source_weight": 1.0,
                "ibkr_scope": scope,
                "extra_data": extra_data[:4000],
            },
        )
        self.on_article(article)
        if not article_id:
            return
        try:
            self._article_requests.put_nowait((article, provider, article_id))
        except queue.Full:
            LOG.warning("IBKR article-body queue full; retained headline %s", article_id)

    def _receive_contract_details(self, details) -> None:
        if self.on_symbol_context is None:
            return
        contract = details.contract
        symbol = str(getattr(contract, "symbol", "")).upper()
        raw_context = " ".join(
            str(getattr(details, field, "") or "")
            for field in ("industry", "category", "subcategory", "longName")
        )
        industries = classify_industries(raw_context)
        if not symbol or not industries:
            return
        try:
            self.on_symbol_context(symbol, industries, ())
            with self._lock:
                self._state["symbol_contexts"] += 1
        except Exception as exc:  # noqa: BLE001 - news subscriptions continue
            LOG.warning("Could not persist IBKR industry context for %s: %s", symbol, exc)

    def _request_articles(self) -> None:
        while not self._stop.is_set():
            try:
                article, provider, article_id = self._article_requests.get(timeout=1.0)
            except queue.Empty:
                continue
            client = self._client
            if client and client.isConnected():
                request_id = self._request_id()
                with self._lock:
                    self._article_pending[request_id] = article
                try:
                    client.reqNewsArticle(request_id, provider, article_id, [])
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._article_pending.pop(request_id, None)
                    LOG.warning("IBKR article request %s failed: %s", article_id, exc)
            self._article_requests.task_done()
            self._stop.wait(0.2)

    def _receive_article(self, request_id: int, article_type: int, text: str) -> None:
        with self._lock:
            article = self._article_pending.pop(request_id, None)
        if article is None:
            return
        body = "" if article_type == 1 else strip_html(text, 100_000)
        enriched = NewsArticle(
            **{
                **article.__dict__,
                "body": body,
                "metadata": {**article.metadata, "article_type": article_type},
            }
        )
        self.on_article(enriched)
        with self._lock:
            self._state["articles"] += 1

    def _receive_error(self, request_id: int, code: int, message: str) -> None:
        if code in _INFORMATIONAL_CODES:
            return
        with self._lock:
            self._state["last_error"] = f"IBKR {code}: {message}"
            self._article_pending.pop(request_id, None)
        # BroadTape support differs by provider. A rejected provider contract is
        # isolated to its request; contract-specific and RSS ingestion continue.
        LOG.warning("IBKR news request %s error %s: %s", request_id, code, message)
