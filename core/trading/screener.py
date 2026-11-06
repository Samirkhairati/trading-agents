"""Cheap, LLM-free candidate selection so only a few stocks get the expensive 12-call analysis."""
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional

import time

import yfinance as yf

SCREENS = ("most_actives", "day_gainers", "day_losers")
MIN_MARKET_CAP = 10e9
MIN_PRICE = 5.0
_cache: Dict[str, object] = {"at": 0.0, "tickers": []}

def fetch_universe(per_screen: int = 25, max_tickers: int = 40) -> List[str]:
    """Live candidate pool from Yahoo's predefined screens (most active, top gainers, top losers), keeping liquid US large caps.
    Any failure just gives a smaller (or empty) list. Cached for an hour so the web UI can reuse it."""
    if time.time() - float(_cache["at"]) < 3600 and _cache["tickers"]:
        return list(_cache["tickers"])  # type: ignore[arg-type]
    seen: Dict[str, None] = {}
    for name in SCREENS:
        try:
            for q in yf.screen(name, count=per_screen).get("quotes", []):
                sym = q.get("symbol", "")
                if (sym and sym.isascii() and "." not in sym and "^" not in sym and q.get("quoteType", "EQUITY") == "EQUITY"
                        and (q.get("marketCap") or 0) >= MIN_MARKET_CAP and (q.get("regularMarketPrice") or 0) >= MIN_PRICE):
                    seen[sym] = None
        except Exception:
            continue
    tickers = list(seen)[:max_tickers]
    if tickers:
        _cache.update(at=time.time(), tickers=tickers)
    return tickers

def score_watchlist(watchlist: Iterable[str]) -> Dict[str, float]:
    """Interest score per ticker: |5-day move| (%) plus volume spike vs 20-day average. Failures score 0."""
    scores: Dict[str, float] = {}
    for t in watchlist:
        try:
            h = yf.Ticker(t).history(period="2mo")
            move = abs(h["Close"].iloc[-1] / h["Close"].iloc[-6] - 1) * 100
            spike = h["Volume"].iloc[-1] / h["Volume"].tail(20).mean()
            scores[t] = float(move + 2 * max(spike - 1, 0))
        except Exception:
            scores[t] = 0.0
    return scores

def pick_candidates(
    scores: Dict[str, float],
    held: Iterable[str],
    last_analysed: Dict[str, str],
    n: int = 3,
    cooldown_days: int = 3,
    today: Optional[date] = None,
    review_days: int = 7,
    max_held: int = 4,
) -> List[str]:
    """Held tickers come first, but each is only re-reviewed every `review_days` (stops cover the gap; daily re-rating
    of the same stock by a small model mostly adds churn), oldest review first, at most `max_held` per cycle.
    Then the top-scoring names from the live universe not analysed recently."""
    today = today or date.today()
    held = list(dict.fromkeys(held))

    def recent(t: str, days: int) -> bool:
        d = last_analysed.get(t)
        return bool(d) and date.fromisoformat(d) > today - timedelta(days=days)

    due = sorted((t for t in held if not recent(t, review_days)), key=lambda t: last_analysed.get(t, ""))[:max_held]
    fresh = [t for t, _ in sorted(scores.items(), key=lambda kv: -kv[1]) if t not in held and not recent(t, cooldown_days)]
    return due + fresh[:n]
