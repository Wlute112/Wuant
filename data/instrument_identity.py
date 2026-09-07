"""Strict identity contract for the supported US-listed USD equity universe."""
from dataclasses import asdict, dataclass
import re

US_EXCHANGES = frozenset({"AMEX", "ARCA", "BATS", "CBOE", "EDGEA", "IEX", "ISLAND",
                           "NASDAQ", "NASDAQ.NMS", "NYSE", "NYSEARCA"})
STOCK_TYPES = frozenset({"COMMON", "ETF", "ADR", "REIT", "PREFERRED"})
SYMBOL = re.compile(r"^[A-Z][A-Z0-9. -]{0,19}$")


@dataclass(frozen=True)
class EquityIdentity:
    con_id: int
    symbol: str
    primary_exchange: str
    stock_type: str
    currency: str = "USD"
    security_type: str = "STK"

    @classmethod
    def from_info(cls, info: dict) -> "EquityIdentity":
        contract = info.get("contract") or {}
        con_id = contract.get("conId")
        if type(con_id) is not int or con_id <= 0:
            raise ValueError("Qualified IBKR conId is missing or invalid")
        if contract.get("secType") != "STK" or contract.get("currency") != "USD":
            raise ValueError("Only USD STK contracts are supported by the equity execution profile")
        exchange = str(contract.get("primaryExchange", "")).upper()
        if exchange not in US_EXCHANGES:
            raise ValueError(f"Unsupported or unverified US primary exchange: {exchange!r}")
        stock_type = str(info.get("stockType", "")).upper()
        if stock_type not in STOCK_TYPES:
            raise ValueError(f"Unsupported or missing IBKR stock type: {stock_type!r}")
        symbol = str(contract.get("symbol", ""))
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("Invalid broker equity symbol")
        return cls(con_id, symbol, exchange, stock_type)

    def as_dict(self) -> dict:
        return asdict(self)


def validate_equity_instrument(instrument) -> bool:
    """Nautilus provider filter: reject unsupported contracts before trading."""
    EquityIdentity.from_info(instrument.info or {})
    return True
