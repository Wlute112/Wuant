"""Deterministic equity industry metadata used by alpha and risk controls.

The classification is deliberately structural: it is fixed before a study and
is never selected by Optuna.  Callers can replace or extend it through strategy
configuration without changing feature construction.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence


DEFAULT_INDUSTRY_BY_SYMBOL: dict[str, str] = {
    # Semiconductors
    **{symbol: "semiconductors" for symbol in (
        "NVDA", "AMD", "AVGO", "QCOM", "INTC", "MU", "TXN", "ADI",
        "MRVL", "MCHP", "ON", "ARM", "SMH", "SOXX",
    )},
    # Software and cloud
    **{symbol: "software_cloud" for symbol in (
        "MSFT", "ORCL", "CRM", "ADBE", "NOW", "INTU", "PLTR", "SNOW",
        "DDOG", "MDB", "IGV", "SKYY",
    )},
    # Cybersecurity
    **{symbol: "cybersecurity" for symbol in (
        "PANW", "CRWD", "FTNT", "ZS", "OKTA", "CYBR", "CIBR", "HACK",
    )},
    # Technology hardware and networking
    **{symbol: "technology_hardware" for symbol in (
        "AAPL", "DELL", "HPQ", "LOGI", "CSCO", "ANET", "HPE", "XLK",
    )},
    # Interactive media and internet platforms
    **{symbol: "internet_media" for symbol in (
        "GOOG", "GOOGL", "META", "NFLX", "PINS", "SNAP", "XLC",
    )},
    # Money-center and investment banks
    **{symbol: "banks" for symbol in (
        "JPM", "BAC", "C", "WFC", "GS", "MS", "USB", "PNC", "XLF",
        "KBE", "KRE",
    )},
    # Energy producers
    **{symbol: "energy_producers" for symbol in (
        "XOM", "CVX", "COP", "OXY", "EOG", "PXD", "DVN", "XLE", "XOP",
    )},
    # Oilfield services
    **{symbol: "oilfield_services" for symbol in (
        "SLB", "HAL", "BKR", "OIH",
    )},
    # Biotechnology and pharmaceuticals
    **{symbol: "biotechnology" for symbol in (
        "AMGN", "GILD", "BIIB", "REGN", "VRTX", "MRNA", "XBI", "IBB",
    )},
    **{symbol: "pharmaceuticals" for symbol in (
        "LLY", "JNJ", "PFE", "MRK", "BMY", "ABBV", "NVO", "IHE", "XLV",
    )},
    # Consumer groups
    **{symbol: "consumer_discretionary" for symbol in (
        "AMZN", "TSLA", "HD", "LOW", "NKE", "SBUX", "MCD", "XLY",
    )},
    **{symbol: "consumer_staples" for symbol in (
        "WMT", "COST", "PG", "KO", "PEP", "MDLZ", "XLP",
    )},
    # Industrial groups
    **{symbol: "aerospace_defense" for symbol in (
        "BA", "LMT", "RTX", "NOC", "GD", "ITA", "XAR",
    )},
    **{symbol: "transportation_logistics" for symbol in (
        "UPS", "FDX", "UNP", "CSX", "DAL", "UAL", "IYT", "JETS",
    )},
    # Materials, utilities, and real estate
    **{symbol: "materials_mining" for symbol in (
        "FCX", "NEM", "NUE", "STLD", "GDX", "XLB",
    )},
    **{symbol: "utilities" for symbol in (
        "NEE", "DUK", "SO", "D", "AEP", "XLU",
    )},
    **{symbol: "real_estate" for symbol in (
        "AMT", "PLD", "EQIX", "O", "SPG", "XLRE",
    )},
}


INDUSTRY_SECTOR: dict[str, str] = {
    "semiconductors": "technology",
    "software_cloud": "technology",
    "cybersecurity": "technology",
    "technology_hardware": "technology",
    "internet_media": "communication_services",
    "banks": "financials",
    "energy_producers": "energy",
    "oilfield_services": "energy",
    "biotechnology": "healthcare",
    "pharmaceuticals": "healthcare",
    "consumer_discretionary": "consumer_discretionary",
    "consumer_staples": "consumer_staples",
    "aerospace_defense": "industrials",
    "transportation_logistics": "industrials",
    "materials_mining": "materials",
    "utilities": "utilities",
    "real_estate": "real_estate",
}


DEFAULT_INDUSTRY_BENCHMARK: dict[str, str] = {
    "semiconductors": "SMH",
    "software_cloud": "IGV",
    "cybersecurity": "CIBR",
    "technology_hardware": "XLK",
    "internet_media": "XLC",
    "banks": "XLF",
    "energy_producers": "XLE",
    "oilfield_services": "OIH",
    "biotechnology": "XBI",
    "pharmaceuticals": "IHE",
    "consumer_discretionary": "XLY",
    "consumer_staples": "XLP",
    "aerospace_defense": "ITA",
    "transportation_logistics": "IYT",
    "materials_mining": "XLB",
    "utilities": "XLU",
    "real_estate": "XLRE",
}


def base_symbol(value: str) -> str:
    """Normalize Nautilus instrument IDs and plain tickers to one symbol key."""
    return str(value).split(".", 1)[0].split("/", 1)[0].upper()


def _normalized_map(
    override: Mapping[str, str] | None,
    default: Mapping[str, str],
) -> dict[str, str]:
    result = {base_symbol(key): str(value) for key, value in default.items()}
    if override:
        result.update({base_symbol(key): str(value) for key, value in override.items()})
    return result


def industry_for_symbol(
    symbol: str,
    industry_map: Mapping[str, str] | None = None,
) -> str | None:
    return _normalized_map(industry_map, DEFAULT_INDUSTRY_BY_SYMBOL).get(
        base_symbol(symbol)
    )


def sector_for_symbol(
    symbol: str,
    industry_map: Mapping[str, str] | None = None,
    sector_map: Mapping[str, str] | None = None,
) -> str | None:
    normalized_sector = _normalized_map(sector_map, {})
    direct = normalized_sector.get(base_symbol(symbol))
    if direct:
        return direct
    industry = industry_for_symbol(symbol, industry_map)
    return INDUSTRY_SECTOR.get(industry) if industry else None


def industry_peers_for_symbol(
    target: str,
    universe: Sequence[str],
    *,
    industry_map: Mapping[str, str] | None = None,
    benchmark_map: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return in-universe same-industry peers in deterministic universe order."""
    classification = _normalized_map(industry_map, DEFAULT_INDUSTRY_BY_SYMBOL)
    target_base = base_symbol(target)
    industry = classification.get(target_base)
    if industry is None:
        return ()

    benchmark_by_industry = dict(DEFAULT_INDUSTRY_BENCHMARK)
    if benchmark_map:
        benchmark_by_industry.update(
            {str(key): base_symbol(value) for key, value in benchmark_map.items()}
        )
    benchmark = benchmark_by_industry.get(industry)
    peers: list[str] = []
    for candidate in universe:
        candidate_base = base_symbol(candidate)
        if candidate_base == target_base:
            continue
        if classification.get(candidate_base) == industry or candidate_base == benchmark:
            peers.append(str(candidate))
    return tuple(peers)
