"""The portfolio as the API / UI show it (one place, used by the public endpoint and the admin page)."""
import os
from typing import Any, Dict

from trading import state as st
from trading.broker import AlpacaBroker
from trading import daily_run
from trading.risk import drawdown_pct

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
        "positions": [
            {"symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry, "price": p.price,
             "market_value": p.market_value, "pnl_pct": p.pnl_pct} for p in positions
        ],
    }
