"""The portfolio as the API / UI show it (one place, used by the public endpoint and the admin page)."""
import os
from typing import Any, Dict, List

from trading import state as st
from trading.broker import AlpacaBroker
from trading import daily_run
from trading.risk import drawdown_pct

def _from_ledger(e: dict) -> dict:
    """One simulated-account ledger entry in the shape the UI shows."""
    kind, price, qty = e["type"], e.get("price"), e.get("qty")
    amount = e.get("amount") if kind == "deposit" else e.get("notional") if kind == "buy" else (qty or 0) * (price or 0)
    note = "cash added" if kind == "deposit" else e.get("reason") or ""
    if kind == "sell" and e.get("source") == "admin":
        note = "closed by hand"
    return {"time": e["time"], "type": kind, "symbol": e.get("symbol"), "qty": qty, "price": price, "amount": amount,
            "pnl": e.get("pnl"), "note": note}

def recent_transactions(broker, limit: int = 50) -> List[dict]:
    """What the account really did (newest first): the simulated ledger, or Alpaca's filled orders. Empty for the demo broker."""
    try:
        if hasattr(broker, "recent_fills"):
            return broker.recent_fills(limit)
        return [_from_ledger(e) for e in reversed(getattr(broker, "ledger", []))][:limit]
    except Exception:  # a broker hiccup must not take the dashboard down
        return []

def portfolio_snapshot() -> Dict[str, Any]:
    broker = daily_run.make_broker()
    account, positions = broker.get_account(), broker.get_positions()
    state = st.load_state()
    peak = max(state["peak_equity"] or 0.0, account.equity)
    demo = os.environ.get("DEMO_PORTFOLIO") == "1"
    return {
        "mode": "DEMO (seeded fake data)" if demo else "alpaca-paper" if isinstance(broker, AlpacaBroker) else "simulated paper account",
        "can_deposit": hasattr(broker, "deposit"),
        "deposited": getattr(broker, "deposited", None),
        "equity_history": state["equity_history"][-365:],
        "equity": account.equity, "cash": account.cash, "peak_equity": peak,
        "drawdown_pct": drawdown_pct(account.equity, peak),
        "ledger": list(reversed(getattr(broker, "ledger", [])))[:100],
        "transactions": recent_transactions(broker),
        "positions": [
            {"symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry, "price": p.price,
             "market_value": p.market_value, "pnl_pct": p.pnl_pct} for p in positions
        ],
    }
