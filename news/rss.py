"""Resilient polling for the controlled RSS/Atom catalog."""
from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import logging
import threading
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    import feedparser
except ImportError:  # pragma: no cover - exercised by deployment dependency checks
    feedparser = None

from quant.news.catalog import RssFeed
from quant.news.core import NewsArticle


LOG = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() in {"script", "style", "nav", "footer", "header"}:
            self._skip += 1

    def handle_endtag(self, tag) -> None:
        if tag.lower() in {"script", "style", "nav", "footer", "header"} and self._skip:
            self._skip -= 1

    def handle_data(self, data) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def strip_html(value: str, limit: int = 20_000) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(str(value or ""))
        text = " ".join(parser.parts)
    except Exception:  # noqa: BLE001 - malformed publisher HTML is non-fatal
        text = str(value or "")
    return " ".join(text.split())[:limit]


def _entry_time(entry, now: datetime) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                value = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
                return min(value, now)
            except (TypeError, ValueError, OverflowError):
                pass
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            value = parsedate_to_datetime(str(raw))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return min(value.astimezone(timezone.utc), now)
        except (TypeError, ValueError, OverflowError):
            pass
    return now


class RssPoller:
    def __init__(
        self,
        feeds: tuple[RssFeed, ...],
        on_article: Callable[[NewsArticle], None],
        *,
        poll_seconds: int = 120,
        request_timeout_seconds: float = 12.0,
        bootstrap_max_age_hours: float = 168.0,
        max_entries_per_feed: int = 50,
    ) -> None:
        self.feeds = feeds
        self.on_article = on_article
        self.poll_seconds = max(int(poll_seconds), 30)
        self.request_timeout_seconds = max(float(request_timeout_seconds), 1.0)
        self.bootstrap_max_age_hours = max(float(bootstrap_max_age_hours), 1.0)
        self.max_entries_per_feed = max(int(max_entries_per_feed), 1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._validators: dict[str, dict[str, str]] = {}
        self._polled: set[str] = set()
        self._lock = threading.Lock()
        self._stats = {"polls": 0, "articles": 0, "errors": {}}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="news-rss", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def status(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "feed_count": len(self.feeds),
                "running": bool(self._thread and self._thread.is_alive()),
            }

    def poll_once(self) -> int:
        if not self.feeds:
            return 0
        count = 0
        with ThreadPoolExecutor(max_workers=min(8, len(self.feeds))) as pool:
            futures = {pool.submit(self._fetch, feed): feed for feed in self.feeds}
            for future in as_completed(futures):
                feed = futures[future]
                try:
                    articles = future.result()
                    for article in articles:
                        self.on_article(article)
                        count += 1
                    with self._lock:
                        self._stats["errors"].pop(feed.name, None)
                except Exception as exc:  # noqa: BLE001 - one feed cannot stop the catalog
                    with self._lock:
                        self._stats["errors"][feed.name] = str(exc)[:500]
                    LOG.warning("RSS feed %s failed: %s", feed.name, exc)
        with self._lock:
            self._stats["polls"] += 1
            self._stats["articles"] += count
        return count

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_seconds)

    def _fetch(self, feed: RssFeed) -> list[NewsArticle]:
        if feedparser is None:
            raise RuntimeError(
                "RSS ingestion requires feedparser; install quant/requirements.txt"
            )
        headers = {
            "User-Agent": "quant-news/1.0 (local research system)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
        }
        headers.update(self._validators.get(feed.url, {}))
        request = Request(feed.url, headers=headers)
        try:
            with urlopen(request, timeout=self.request_timeout_seconds) as response:
                data = response.read(4_000_000)
                validators = {}
                if response.headers.get("ETag"):
                    validators["If-None-Match"] = response.headers["ETag"]
                if response.headers.get("Last-Modified"):
                    validators["If-Modified-Since"] = response.headers["Last-Modified"]
                if validators:
                    self._validators[feed.url] = validators
        except HTTPError as exc:
            if exc.code == 304:
                return []
            raise

        parsed = feedparser.parse(data)
        if parsed.bozo and not parsed.entries:
            raise ValueError(str(parsed.get("bozo_exception") or "invalid feed"))
        now = datetime.now(timezone.utc)
        first_poll = feed.url not in self._polled
        self._polled.add(feed.url)
        articles = []
        for entry in parsed.entries[: self.max_entries_per_feed]:
            title = strip_html(entry.get("title", ""), 2000)
            if not title:
                continue
            published = _entry_time(entry, now)
            age_hours = max((now - published).total_seconds() / 3600.0, 0.0)
            if first_poll and age_hours > self.bootstrap_max_age_hours:
                continue
            content = entry.get("content") or []
            body_html = content[0].get("value", "") if content else ""
            summary = strip_html(entry.get("summary", entry.get("description", "")))
            body = strip_html(body_html, 50_000)
            link = str(entry.get("link", ""))
            external_id = str(entry.get("id", entry.get("guid", link)))
            articles.append(
                NewsArticle(
                    source_kind="rss",
                    source_name=feed.name,
                    title=title,
                    published_at=published,
                    received_at=now,
                    external_id=external_id,
                    summary=summary,
                    body=body,
                    url=link,
                    industries=feed.industries,
                    commodities=feed.commodities,
                    metadata={"source_weight": feed.weight, "feed_url": feed.url},
                )
            )
        return articles
