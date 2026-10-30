"""Cheap, LLM-free candidate selection so only a few stocks get the expensive 12-call analysis."""
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional

import yfinance as yf

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
) -> List[str]:
    """Held tickers always come first (they may need a sell). Then top-scoring watchlist names not analysed recently."""
    today = today or date.today()
    held = list(dict.fromkeys(held))
    cutoff = today - timedelta(days=cooldown_days)

    def recent(t: str) -> bool:
        d = last_analysed.get(t)
        return bool(d) and date.fromisoformat(d) > cutoff

    fresh = [t for t, _ in sorted(scores.items(), key=lambda kv: -kv[1]) if t not in held and not recent(t)]
    return held + fresh[:n]
