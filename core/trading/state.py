"""JSON persistence for the paper-trading loop: trade log + small portfolio state, one set of files per market.
US files sit in the state root (as they always did); the other markets use a sub-directory named after the market code."""
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from paths import state_dir

def _dir(market: str = "us") -> Path:
    base = state_dir()
    if os.environ.get("DEMO_PORTFOLIO") == "1":
        base = base / "demo"  # seeded fake data lives apart from the real state
    root = Path(os.environ.get("TRADING_STATE_DIR", base))
    return root if market == "us" else root / market

def _read(name: str, default: Any, market: str = "us") -> Any:
    try:
        return json.loads((_dir(market) / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def _write(name: str, data: Any, market: str = "us") -> None:
    _dir(market).mkdir(parents=True, exist_ok=True)
    (_dir(market) / name).write_text(json.dumps(data, indent=2, default=str))

def load_state(market: str = "us") -> Dict[str, Any]:
    """{peak_equity, last_analysed: {ticker: iso date}, equity_history: [{date, equity, cash}]}"""
    s = _read("portfolio_state.json", {}, market)
    s.setdefault("peak_equity", None)
    s.setdefault("last_analysed", {})
    s.setdefault("equity_history", [])
    return s

def save_state(state: Dict[str, Any], market: str = "us") -> None:
    _write("portfolio_state.json", state, market)

def record_equity(state: Dict[str, Any], equity: float, cash: float) -> None:
    """One equity point per day (latest run of the day wins); also tracks the peak."""
    today = date.today().isoformat()
    hist = [h for h in state["equity_history"] if h["date"] != today]
    hist.append({"date": today, "equity": equity, "cash": cash})
    state["equity_history"] = hist
    state["peak_equity"] = max(equity, state["peak_equity"] or 0.0)

def log_run(entry: Dict[str, Any], market: str = "us") -> None:
    """Append one daily-run record (candidates, plan, approved/rejected orders, fills) to trades.json."""
    runs: List[Dict[str, Any]] = _read("trades.json", [], market)
    runs.append(entry)
    _write("trades.json", runs, market)

def get_runs(market: str = "us") -> List[Dict[str, Any]]:
    return _read("trades.json", [], market)
