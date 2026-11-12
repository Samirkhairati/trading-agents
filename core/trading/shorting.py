"""When is it sensible to short? Shorts have unlimited downside, so a Sell rating alone is not enough.
`short_blockers` (pure) lists every reason NOT to open a short; the daily run only lets a short through when the list is empty.
`fetch_short_facts` is the only I/O and fails soft (an unknown fact never blocks, missing price history always does)."""
import time
from typing import Any, Dict, List, Optional

import yfinance as yf

SHORT_RATING = "Sell"  # Underweight is not enough conviction to risk unlimited loss
MAX_ANALYST_FAVOURITE = 2.2  # Yahoo's 1 (strong buy) .. 5 (sell) consensus; below this Wall Street likes the stock too much
MAX_SHORT_FLOAT_PCT = 20.0  # heavily shorted names get squeezed
MAX_DAYS_TO_COVER = 7.0
MAX_MONTH_RETURN_PCT = 10.0  # do not stand in front of a rally
MIN_EARNINGS_DAYS = 5  # earnings gaps can blow through any stop

def fetch_short_facts(symbol: str) -> Dict[str, Any]:
    """price, 50-day average, 1-month return, analyst consensus, short interest and days to the next earnings (None when unknown)."""
    facts: Dict[str, Any] = {"price": None, "sma50": None, "ret_1m": None, "analyst": None, "short_float_pct": None, "days_to_cover": None, "earnings_days": None}
    t = yf.Ticker(symbol)
    try:
        close = t.history(period="4mo")["Close"].dropna()
        facts["price"] = float(close.iloc[-1])
        facts["sma50"] = float(close.tail(50).mean()) if len(close) >= 30 else None
        facts["ret_1m"] = float(close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) > 22 else None
    except Exception:
        pass
    try:
        info = t.info or {}
        facts["analyst"] = info.get("recommendationMean")
        if info.get("shortPercentOfFloat") is not None:
            facts["short_float_pct"] = float(info["shortPercentOfFloat"]) * 100
        facts["days_to_cover"] = info.get("shortRatio")
        ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if ts and ts > time.time():
            facts["earnings_days"] = (ts - time.time()) / 86400
    except Exception:
        pass
    return facts

def short_blockers(rating: str, facts: Dict[str, Any], shortable: Optional[bool] = None) -> List[str]:
    """Reasons not to short this stock now; an empty list means the situation is right.
    The situation: rated Sell, already in a downtrend (below its 50-day average, not bouncing), not a crowded short,
    not an analyst favourite, no earnings in the next days, and the broker can borrow it."""
    why: List[str] = []
    if rating != SHORT_RATING:
        why.append(f"rating is {rating}, shorts need {SHORT_RATING}")
    if shortable is False:
        why.append("the broker cannot borrow this stock")
    if facts.get("price") is None:
        why.append("no price history")
        return why
    sma, ret = facts.get("sma50"), facts.get("ret_1m")
    if sma is not None and facts["price"] >= sma:
        why.append(f"price {facts['price']:,.2f} is above its 50-day average {sma:,.2f} (no confirmed downtrend)")
    if ret is not None and ret > MAX_MONTH_RETURN_PCT:
        why.append(f"up {ret:.1f}% in a month (rallying)")
    if facts.get("analyst") is not None and facts["analyst"] < MAX_ANALYST_FAVOURITE:
        why.append(f"analyst consensus is {facts['analyst']:.1f}/5 (a Wall Street favourite)")
    if facts.get("short_float_pct") is not None and facts["short_float_pct"] >= MAX_SHORT_FLOAT_PCT:
        why.append(f"{facts['short_float_pct']:.0f}% of the float is already short (squeeze risk)")
    if facts.get("days_to_cover") is not None and facts["days_to_cover"] >= MAX_DAYS_TO_COVER:
        why.append(f"{facts['days_to_cover']:.1f} days to cover (squeeze risk)")
    if facts.get("earnings_days") is not None and facts["earnings_days"] < MIN_EARNINGS_DAYS:
        why.append(f"earnings in {facts['earnings_days']:.0f} days (gap risk)")
    return why
