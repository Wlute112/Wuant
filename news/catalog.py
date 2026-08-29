"""Default RSS catalog and the controlled market taxonomy used by news scoring."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


INDUSTRIES = (
    "aerospace_defense",
    "agriculture",
    "automotive",
    "banking_finance",
    "biotechnology",
    "chemicals",
    "clean_energy",
    "consumer_discretionary",
    "consumer_staples",
    "cybersecurity",
    "energy",
    "healthcare",
    "industrials",
    "insurance",
    "materials_mining",
    "media_communications",
    "pharmaceuticals",
    "real_estate",
    "retail",
    "semiconductors",
    "technology",
    "transportation_logistics",
    "utilities",
)

COMMODITIES = (
    "aluminum",
    "cattle",
    "coal",
    "cocoa",
    "coffee",
    "copper",
    "corn",
    "cotton",
    "crude_oil",
    "gold",
    "lithium",
    "natural_gas",
    "nickel",
    "silver",
    "soybeans",
    "sugar",
    "uranium",
    "wheat",
)


@dataclass(frozen=True)
class RssFeed:
    name: str
    url: str
    industries: tuple[str, ...] = ()
    commodities: tuple[str, ...] = ()
    weight: float = 1.0


# Endpoints are RSS/Atom interfaces published by the named organizations.  The
# catalog intentionally favors first-party releases for timestamp provenance,
# with several broad/niche publications added for coverage between releases.
DEFAULT_RSS_FEEDS = (
    RssFeed("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", ("banking_finance",), weight=1.0),
    RssFeed("SEC", "https://www.sec.gov/news/pressreleases.rss", ("banking_finance",), weight=1.0),
    RssFeed("BEA", "https://apps.bea.gov/rss/rss.xml", weight=1.0),
    RssFeed("Census Economic Indicators", "https://www.census.gov/economic-indicators/indicator.xml", weight=1.0),
    RssFeed("Bureau of Labor Statistics", "https://www.bls.gov/feed/bls_latest.rss", ("banking_finance", "consumer_discretionary", "industrials"), weight=1.0),
    RssFeed("EIA Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml", ("energy", "utilities"), ("crude_oil", "natural_gas", "coal"), 1.0),
    RssFeed("EIA Press Releases", "https://www.eia.gov/rss/press_rss.xml", ("energy", "utilities"), ("crude_oil", "natural_gas", "coal"), 1.0),
    RssFeed("EIA This Week in Petroleum", "https://www.eia.gov/petroleum/weekly/includes/week_in_petroleum_rss.xml", ("energy", "transportation_logistics"), ("crude_oil",), 1.0),
    RssFeed("EIA Gasoline and Diesel", "https://www.eia.gov/petroleum/gasdiesel/includes/gas_diesel_rss.xml", ("energy", "transportation_logistics"), ("crude_oil",), 1.0),
    RssFeed("EIA Heating Oil and Propane", "https://www.eia.gov/petroleum/heatingoilpropane/includes/hopu_rss.xml", ("energy", "utilities"), ("crude_oil", "natural_gas"), 1.0),
    RssFeed("USDA Agricultural Marketing Service", "https://www.ams.usda.gov/rss.xml", ("agriculture", "consumer_staples"), ("cattle", "corn", "soybeans", "wheat"), 1.0),
    RssFeed("USDA NASS", "https://www.nass.usda.gov/rss/news.xml", ("agriculture",), ("cattle", "corn", "soybeans", "wheat", "cotton"), 1.0),
    RssFeed("Department of Energy", "https://www.energy.gov/rss.xml", ("energy", "clean_energy", "utilities"), ("uranium", "natural_gas", "crude_oil"), 1.0),
    RssFeed("FDA Press Announcements", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml", ("healthcare", "pharmaceuticals", "biotechnology"), weight=1.0),
    RssFeed("CMS Newsroom", "https://www.cms.gov/newsroom/rss-feeds", ("healthcare", "insurance", "pharmaceuticals"), weight=1.0),
    RssFeed("FTC Press Releases", "https://www.ftc.gov/feeds/press-release.xml", ("technology", "retail", "consumer_discretionary"), weight=1.0),
    RssFeed("CISA Advisories", "https://www.cisa.gov/cybersecurity-advisories/all.xml", ("cybersecurity", "technology"), weight=1.0),
    RssFeed("NASA News Releases", "https://www.nasa.gov/news-release/feed/", ("aerospace_defense", "technology"), weight=0.9),
    RssFeed("Defense News", "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml", ("aerospace_defense", "industrials"), weight=0.8),
    RssFeed("Semiconductor Engineering", "https://semiengineering.com/feed/", ("semiconductors", "technology"), ("copper",), 0.8),
    RssFeed("Fierce Biotech", "https://www.fiercebiotech.com/rss/xml", ("biotechnology", "pharmaceuticals", "healthcare"), weight=0.8),
    RssFeed("Utility Dive", "https://www.utilitydive.com/feeds/news/", ("utilities", "energy", "clean_energy"), ("natural_gas", "coal", "uranium"), 0.8),
    RssFeed("Retail Dive", "https://www.retaildive.com/feeds/news/", ("retail", "consumer_discretionary", "consumer_staples"), weight=0.8),
    RssFeed("Insurance Journal", "https://www.insurancejournal.com/rss/news/", ("insurance", "banking_finance", "real_estate"), weight=0.8),
    RssFeed("CleanTechnica", "https://cleantechnica.com/feed/", ("clean_energy", "automotive", "utilities", "technology"), ("lithium", "nickel", "copper"), 0.75),
    RssFeed("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", ("banking_finance", "technology"), weight=0.75),
    RssFeed("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", weight=0.85),
    RssFeed("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", weight=0.8),
    RssFeed("NPR Business", "https://feeds.npr.org/1006/rss.xml", weight=0.8),
    RssFeed("Guardian Business", "https://www.theguardian.com/us/business/rss", weight=0.8),
    RssFeed("FreightWaves", "https://www.freightwaves.com/feed", ("transportation_logistics", "industrials"), weight=0.8),
    RssFeed("OilPrice", "https://oilprice.com/rss/main", ("energy", "materials_mining"), ("crude_oil", "natural_gas", "uranium"), 0.8),
)


def load_rss_catalog(path: str | None = None) -> tuple[RssFeed, ...]:
    """Return the built-in catalog, optionally extended/replaced by JSON.

    The JSON format is either a list of feed objects or
    ``{"replace_defaults": bool, "feeds": [...]}``.
    """
    if not path:
        return DEFAULT_RSS_FEEDS
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    replace = isinstance(payload, dict) and bool(payload.get("replace_defaults"))
    rows = payload.get("feeds", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("RSS catalog must contain a list of feeds")
    custom = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name") or not row.get("url"):
            raise ValueError("Each RSS feed requires name and url")
        industries = tuple(str(v) for v in row.get("industries", ()))
        commodities = tuple(str(v) for v in row.get("commodities", ()))
        unknown_industries = set(industries) - set(INDUSTRIES)
        unknown_commodities = set(commodities) - set(COMMODITIES)
        if unknown_industries or unknown_commodities:
            raise ValueError(
                f"Unknown RSS taxonomy values: {sorted(unknown_industries | unknown_commodities)}"
            )
        custom.append(
            RssFeed(
                name=str(row["name"]),
                url=str(row["url"]),
                industries=industries,
                commodities=commodities,
                weight=min(max(float(row.get("weight", 1.0)), 0.0), 2.0),
            )
        )
    base = () if replace else DEFAULT_RSS_FEEDS
    by_url = {feed.url: feed for feed in (*base, *custom)}
    return tuple(by_url.values())
