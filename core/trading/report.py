"""Performance report: portfolio equity curve vs buy-and-hold SPY since the first logged day.

    python -m trading.report
"""
from datetime import date
from typing import Any, Dict, List

import yfinance as yf

from trading import state as st

def equity_stats(history: List[Dict[str, Any]]) -> Dict[str, float]:
    """Total return and max drawdown (both in %) from [{date, equity}, ...]."""
    eq = [h["equity"] for h in history]
    peak, max_dd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)
    return {"return_pct": (eq[-1] / eq[0] - 1) * 100, "max_drawdown_pct": max_dd}

def win_rate(runs: List[Dict[str, Any]]) -> str:
    sells = [o for r in runs if r.get("executed") for o in r.get("approved", []) if o["side"] == "sell"]
    return f"{len(sells)} executed sells logged" if sells else "no closed trades yet"

def main() -> None:
    state = st.load_state()
    hist = state["equity_history"]
    if len(hist) < 2:
        print(f"Need at least 2 logged days of equity to report (have {len(hist)}). Run the daily loop first.")
        return
    mine = equity_stats(hist)
    start = date.fromisoformat(hist[0]["date"])
    spy = yf.Ticker("SPY").history(start=start.isoformat())["Close"]
    spy_ret = (spy.iloc[-1] / spy.iloc[0] - 1) * 100 if len(spy) > 1 else float("nan")
    print(f"Period: {hist[0]['date']} -> {hist[-1]['date']} ({len(hist)} logged days)")
    print(f"Portfolio: {mine['return_pct']:+.2f}%  (max drawdown {mine['max_drawdown_pct']:.2f}%)")
    print(f"SPY buy-and-hold: {spy_ret:+.2f}%")
    print(f"Trades: {win_rate(st.get_runs())}")
    print("Note: weeks of paper results say very little. Not financial advice.")

if __name__ == "__main__":
    main()
