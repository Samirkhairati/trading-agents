"""Seeded FAKE portfolio so the dashboards have something to show. Never touches Alpaca.

    DEMO_PORTFOLIO=1 python -m trading.demo            # (re)seed data/demo with random holdings + 90 days of history
    DEMO_PORTFOLIO=1 docker compose up -d              # the API then serves the demo portfolio and its gauges
"""
import json
import math
import os
import random
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import observability as obs
from trading import state as st
from trading.broker import FakeBroker

BASE_PRICES = {
    "AAPL": 230.0, "MSFT": 440.0, "NVDA": 205.0, "AMZN": 215.0, "GOOGL": 185.0, "META": 620.0,
    "TSLA": 330.0, "JPM": 245.0, "XOM": 112.0, "UNH": 510.0, "COST": 960.0, "LLY": 780.0,
}
RATINGS = ["Buy", "Overweight", "Hold", "Underweight"]

def _path():
    return st._dir() / "demo_portfolio.json"

def load_broker() -> FakeBroker:
    """FakeBroker holding the seeded portfolio (seeds it first if the file is missing). Orders are not persisted."""
    if not _path().exists():
        seed()
    d = json.loads(_path().read_text())
    b = FakeBroker(cash=d["cash"], prices=d["prices"])
    b.holdings = {s: (h["qty"], h["avg_entry"]) for s, h in d["holdings"].items()}
    return b

def _weekdays_back(n: int) -> list:
    out, d = [], date.today()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]

def seed(days: int = 90, rng_seed: Optional[int] = None, n_positions: int = 6) -> Dict[str, Any]:
    """Write random holdings, an equity curve ending at today's equity, and a few sample runs. Returns a summary."""
    rng = random.Random(rng_seed)
    equity = rng.uniform(95_000, 130_000)
    cash_pct = rng.uniform(10, 25)
    symbols = rng.sample(sorted(BASE_PRICES), n_positions)
    raw = [rng.uniform(0.5, 2.0) for _ in symbols]
    weights = [r / sum(raw) * (100 - cash_pct) for r in raw]  # percent of equity per position

    prices, holdings = {}, {}
    for sym, w in zip(symbols, weights):
        price = BASE_PRICES[sym] * rng.uniform(0.9, 1.1)
        pnl = rng.uniform(-0.08, 0.25)
        qty = round(equity * w / 100 / price)
        qty = max(qty, 1)
        prices[sym] = round(price, 2)
        holdings[sym] = {"qty": qty, "avg_entry": round(price / (1 + pnl), 2)}
    invested = sum(h["qty"] * prices[s] for s, h in holdings.items())
    cash = round(equity - invested, 2)
    equity = cash + invested

    # random walk ending exactly at today's equity
    rets = [rng.gauss(0.0008, 0.011) for _ in range(days - 1)]
    curve = [equity]
    for r in reversed(rets):
        curve.append(curve[-1] / (1 + r))
    curve = curve[::-1]
    dates = _weekdays_back(days)
    cash_share = cash / equity
    state = {
        "peak_equity": max(curve), "last_analysed": {s: dates[-2].isoformat() for s in symbols[:3]},
        "equity_history": [{"date": d.isoformat(), "equity": round(v, 2), "cash": round(v * cash_share, 2)} for d, v in zip(dates, curve)],
    }
    st.save_state(state)
    st._write("demo_portfolio.json", {"cash": cash, "prices": prices, "holdings": holdings})
    st._write("trades.json", _sample_runs(rng, symbols, prices, equity, cash))
    summary = {"equity": round(equity, 2), "cash": cash, "positions": {s: round(w, 1) for s, w in zip(symbols, weights)},
               "days": days, "start_equity": round(curve[0], 2), "peak": round(max(curve), 2)}
    _trace_seed(summary)
    _publish(equity, cash, len(symbols), state["peak_equity"])
    return summary

def _sample_runs(rng: random.Random, symbols: list, prices: dict, equity: float, cash: float) -> list:
    runs = []
    for i, days_ago in enumerate((14, 7, 1)):
        t = datetime.now() - timedelta(days=days_ago)
        picks = rng.sample(symbols, 3)
        decisions = {s: {"rating": rng.choice(RATINGS), "target_price": round(prices[s] * 1.12, 2), "stop_loss": round(prices[s] * 0.93, 2),
                         "justification": "[demo] Sample decision generated for the seeded portfolio, not a real analysis."} for s in picks}
        buy = picks[0]
        order = {"symbol": buy, "side": "buy", "notional": round(equity * 0.05, 2), "entry_price": prices[buy], "stop_loss": round(prices[buy] * 0.93, 2),
                 "take_profit": round(prices[buy] * 1.12, 2), "reason": "[demo] sample order"}
        runs.append({"time": t.isoformat(timespec="seconds"), "executed": i != 2, "equity": round(equity, 2), "cash": cash, "candidates": picks,
                     "decisions": decisions, "errors": {}, "summary": "[demo] seeded sample run", "approved": [order],
                     "rejected": [{**order, "symbol": picks[1], "reason": "[demo] would exceed the 10% single-position limit"}], "fills": []})
    return runs

def _publish(equity: float, cash: float, n: int, peak: float) -> None:
    obs.EQUITY.set(equity)
    obs.CASH.set(cash)
    obs.PEAK.set(peak)
    obs.DRAWDOWN.set(max(0.0, (peak - equity) / peak * 100))
    obs.POSITIONS.set(n)

def _trace_seed(summary: Dict[str, Any]) -> None:
    """One clearly labelled trace in Langfuse so the seed shows up there (no LLM is involved)."""
    with obs.span("demo_portfolio_seed", kind="span", **{"langfuse.observation.output": json.dumps(summary)}) as root:
        obs.tag_trace(root, "DEMO portfolio seed", demo=True, note="seeded fake data, no real analysis")

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    os.environ["DEMO_PORTFOLIO"] = "1"  # the CLI always writes to the demo dir, never the real state
    obs.init_tracing()
    print(json.dumps(seed(), indent=2))
    obs.shutdown()
