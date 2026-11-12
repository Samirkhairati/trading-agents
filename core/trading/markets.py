"""The markets the portfolio trades. Each has its own simulated account, state files, schedule and currency.
US keeps its files in the state root (as before); every other market lives in a sub-directory named after its code."""
import os
from dataclasses import dataclass
from typing import Tuple

@dataclass(frozen=True)
class Market:
    code: str
    label: str
    flag: str
    currency: str  # symbol the UI prints
    tz: str  # IANA zone of the exchange
    daily_at: str  # default local time of the daily cycle (after the close); env DAILY_AT (US) / DAILY_AT_<CODE>
    benchmark: str  # yfinance symbol for the buy-and-hold comparison
    start_cash: float  # default size of the simulated account (env SIM_START_CASH / SIM_START_CASH_<CODE>)
    min_order: float  # smallest order the risk gate accepts, in the local currency
    min_cap: float  # universe filter: market cap
    min_price: float  # universe filter: share price
    suffixes: Tuple[str, ...]  # yfinance ticker suffixes that belong to this market ("" = none)
    region: str  # Yahoo screener region
    exchanges: Tuple[str, ...]  # Yahoo screener exchange codes

MARKETS = {
    "us": Market("us", "US", "🇺🇸", "$", "America/New_York", "16:30", "SPY", 100_000.0, 50.0, 10e9, 5.0, (), "us", ("NMS", "NYQ")),
    "in": Market("in", "India", "🇮🇳", "₹", "Asia/Kolkata", "16:00", "^NSEI", 10_000_000.0, 5_000.0, 5e11, 20.0, (".NS",), "in", ("NSI",)),
}

def get(code: str = "us") -> Market:
    """The market for a code ('us' / 'in'); unknown codes raise ValueError so a typo never silently trades the wrong account."""
    try:
        return MARKETS[(code or "us").lower()]
    except KeyError:
        raise ValueError(f"unknown market: {code!r} (use one of {', '.join(MARKETS)})") from None

def enabled() -> list:
    """Markets that are scheduled and shown: env MARKETS (default 'us,in'); a code that does not exist is ignored."""
    codes = [c.strip().lower() for c in os.environ.get("MARKETS", "us,in").split(",")]
    return [MARKETS[c] for c in codes if c in MARKETS] or [MARKETS["us"]]

def market_of(ticker: str) -> str:
    """Which market a ticker belongs to, from its yfinance suffix (RELIANCE.NS -> 'in', AAPL -> 'us')."""
    t = ticker.upper()
    return next((m.code for m in MARKETS.values() if m.suffixes and t.endswith(m.suffixes)), "us")

def _env_name(base: str, m: Market) -> str:
    return base if m.code == "us" else f"{base}_{m.code.upper()}"

def daily_at(m: Market) -> Tuple[int, int]:
    """(hour, minute) of the daily cycle in the market's own time zone."""
    h, mi = os.environ.get(_env_name("DAILY_AT", m), m.daily_at).split(":")
    return int(h), int(mi)

def start_cash(m: Market) -> float:
    return float(os.environ.get(_env_name("SIM_START_CASH", m), m.start_cash))

def public(m: Market) -> dict:
    """What the UI needs to know about a market."""
    return {"code": m.code, "label": m.label, "flag": m.flag, "currency": m.currency, "tz": m.tz, "benchmark": m.benchmark}
