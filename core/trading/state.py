"""JSON persistence for the paper-trading loop: trade log + small portfolio state."""
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

def _dir() -> Path:
    return Path(os.environ.get("TRADING_STATE_DIR", Path(__file__).resolve().parent.parent / "data"))

def _read(name: str, default: Any) -> Any:
    try:
        return json.loads((_dir() / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def _write(name: str, data: Any) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    (_dir() / name).write_text(json.dumps(data, indent=2, default=str))

def load_state() -> Dict[str, Any]:
    """{peak_equity, last_analysed: {ticker: iso date}, equity_history: [{date, equity, cash}]}"""
    s = _read("portfolio_state.json", {})
    s.setdefault("peak_equity", None)
    s.setdefault("last_analysed", {})
    s.setdefault("equity_history", [])
    return s

def save_state(state: Dict[str, Any]) -> None:
    _write("portfolio_state.json", state)

def record_equity(state: Dict[str, Any], equity: float, cash: float) -> None:
    """One equity point per day (latest run of the day wins); also tracks the peak."""
    today = date.today().isoformat()
    hist = [h for h in state["equity_history"] if h["date"] != today]
    hist.append({"date": today, "equity": equity, "cash": cash})
    state["equity_history"] = hist
    state["peak_equity"] = max(equity, state["peak_equity"] or 0.0)

def log_run(entry: Dict[str, Any]) -> None:
    """Append one daily-run record (candidates, plan, approved/rejected orders, fills) to trades.json."""
    runs: List[Dict[str, Any]] = _read("trades.json", [])
    runs.append(entry)
    _write("trades.json", runs)

def get_runs() -> List[Dict[str, Any]]:
    return _read("trades.json", [])
