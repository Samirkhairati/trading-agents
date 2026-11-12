"""Cheap, LLM-free candidate selection so only a few stocks get the expensive analysis. Works per market (US, India).
Three sources: market movers (most active / gainers / losers / value / growth), analyst consensus (recommended buys and sells
from Yahoo's analyst rating on the large caps), and the stocks we already hold."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import time

import yfinance as yf
from yfinance import EquityQuery

from trading import markets
from trading.markets import Market

US_SCREENS = ("most_actives", "day_gainers", "day_losers", "undervalued_large_caps", "undervalued_growth_stocks", "growth_technology_stocks")
QUERY_SCREENS = {"most_actives": ("dayvolume", False), "day_gainers": ("percentchange", False), "day_losers": ("percentchange", True)}
ANALYST_POOL = ("intradaymarketcap", False)  # the largest companies, which are the ones analysts cover
ANALYST_BUY_MAX = 2.0  # Yahoo consensus: 1 strong buy .. 3 hold .. 5 sell
ANALYST_SELL_MIN = 3.2
_cache: Dict[str, Dict[str, Any]] = {}  # market -> {"at", "universe": [symbols], "quotes": {symbol: meta}}

def _rating(text: Optional[str]) -> Optional[float]:
    """'2.3 - Buy' -> 2.3"""
    try:
        return float(str(text).split(" - ")[0])
    except ValueError:
        return None

def _meta(q: dict) -> Dict[str, Any]:
    return {"price": q.get("regularMarketPrice"), "analyst": _rating(q.get("averageAnalystRating")), "sma50": q.get("fiftyDayAverage"),
            "change_pct": q.get("regularMarketChangePercent"), "cap": q.get("marketCap")}

def _keep(m: Market, q: dict) -> bool:
    sym = q.get("symbol", "")
    if not sym or not sym.isascii() or q.get("quoteType", "EQUITY") != "EQUITY":
        return False
    if m.suffixes:
        if not sym.endswith(m.suffixes):
            return False
    elif "." in sym or "^" in sym:
        return False
    return (q.get("marketCap") or 0) >= m.min_cap and (q.get("regularMarketPrice") or 0) >= m.min_price

def _quotes(m: Market, name: str, count: int) -> List[dict]:
    """One screen's quotes. US uses Yahoo's ready-made screens where there is one; everything else is an EquityQuery for the market."""
    if m.code == "us" and name in US_SCREENS:
        return yf.screen(name, count=count).get("quotes", [])
    field, asc = QUERY_SCREENS.get(name, ANALYST_POOL)
    query = EquityQuery("and", [EquityQuery("eq", ["region", m.region]), EquityQuery("is-in", ["exchange", *m.exchanges]),
                                EquityQuery("gt", ["intradaymarketcap", m.min_cap])])
    return yf.screen(query, sortField=field, sortAsc=asc, size=count).get("quotes", [])

def fetch_market(market: str = "us", per_screen: int = 25, max_tickers: int = 60) -> Dict[str, Any]:
    """Live pool for a market, cached for an hour so the web UI can reuse it: `universe` (movers, to be scored) and `quotes`
    (metadata incl. analyst rating for everything the screens returned, including the large-cap analyst pool).
    Any failing screen just gives a smaller (or empty) pool."""
    m = markets.get(market)
    hit = _cache.get(m.code)
    if hit and time.time() - hit["at"] < 3600 and hit["quotes"]:
        return hit
    mover_sources = US_SCREENS if m.code == "us" else tuple(QUERY_SCREENS)
    quotes: Dict[str, Dict[str, Any]] = {}
    universe: Dict[str, None] = {}
    for name in (*mover_sources, "analyst_pool"):
        try:
            for q in _quotes(m, name, 100 if name == "analyst_pool" else per_screen):
                if _keep(m, q):
                    quotes[q["symbol"]] = _meta(q)
                    if name != "analyst_pool":
                        universe[q["symbol"]] = None
        except Exception:
            continue
    out = {"at": time.time(), "universe": list(universe)[:max_tickers], "quotes": quotes}
    if quotes:
        _cache[m.code] = out
    return out

def fetch_universe(market: str = "us", per_screen: int = 25, max_tickers: int = 60) -> List[str]:
    """The movers to score: most active, gainers, losers and (US) value / growth screens, keeping liquid large caps."""
    return list(fetch_market(market, per_screen, max_tickers)["universe"])

def analyst_signals(quotes: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    """Signed analyst conviction per ticker: 3 - consensus rating, so positive = analysts recommend buying, negative = selling."""
    return {s: round(3 - q["analyst"], 2) for s, q in quotes.items() if q.get("analyst")}

def _one_score(t: str) -> float:
    try:
        h = yf.Ticker(t).history(period="2mo")
        move = abs(h["Close"].iloc[-1] / h["Close"].iloc[-6] - 1) * 100
        spike = h["Volume"].iloc[-1] / h["Volume"].tail(20).mean()
        return float(move + 2 * max(spike - 1, 0))
    except Exception:
        return 0.0

def score_watchlist(watchlist: Iterable[str]) -> Dict[str, float]:
    """Interest score per ticker: |5-day move| (%) plus volume spike vs 20-day average. Failures score 0."""
    tickers = list(watchlist)
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(tickers, pool.map(_one_score, tickers)))

def pick_with_reasons(
    scores: Dict[str, float],
    held: Iterable[str],
    last_analysed: Dict[str, str],
    n: int = 3,
    cooldown_days: int = 3,
    today: Optional[date] = None,
    review_days: int = 7,
    max_held: int = 4,
    analyst: Optional[Dict[str, float]] = None,
    n_buy: int = 0,
    n_sell: int = 0,
) -> List[Tuple[str, str]]:
    """(ticker, why) in this order: held tickers due for review (every `review_days`, oldest first, at most `max_held`; stops cover
    the gap), analyst-recommended buys, analyst-recommended sells (short candidates), then the top movers.
    Everything but held tickers skips names analysed in the last `cooldown_days`."""
    today = today or date.today()
    held = list(dict.fromkeys(held))
    analyst = analyst or {}

    def recent(t: str, days: int) -> bool:
        d = last_analysed.get(t)
        return bool(d) and date.fromisoformat(d) > today - timedelta(days=days)

    out: List[Tuple[str, str]] = [(t, "held, due for review") for t in
                                  sorted((t for t in held if not recent(t, review_days)), key=lambda t: last_analysed.get(t, ""))[:max_held]]
    taken = set(held)

    def take(ranked: List[Tuple[str, float]], k: int, why) -> None:
        added = 0
        for t, s in ranked:
            if added >= k:
                break
            if t not in taken and not recent(t, cooldown_days):
                out.append((t, why(s)))
                taken.add(t)
                added += 1

    take(sorted(((t, s) for t, s in analyst.items() if s >= 3 - ANALYST_BUY_MAX), key=lambda kv: -kv[1]), n_buy,
         lambda s: f"analyst recommended buy ({3 - s:.1f}/5)")
    take(sorted(((t, s) for t, s in analyst.items() if s <= 3 - ANALYST_SELL_MIN), key=lambda kv: kv[1]), n_sell,
         lambda s: f"analyst recommended sell ({3 - s:.1f}/5)")
    take(sorted(scores.items(), key=lambda kv: -kv[1]), n, lambda s: "top mover / volume spike")
    return out

def pick_candidates(scores: Dict[str, float], held: Iterable[str], last_analysed: Dict[str, str], n: int = 3, cooldown_days: int = 3,
                    today: Optional[date] = None, review_days: int = 7, max_held: int = 4, **analyst_opts: Any) -> List[str]:
    """Just the tickers of `pick_with_reasons`."""
    return [t for t, _ in pick_with_reasons(scores, held, last_analysed, n, cooldown_days, today, review_days, max_held, **analyst_opts)]
