"""Tiny JSON decision log. Lets the Portfolio Manager see how its last call on a ticker played out."""
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PATH = Path(__file__).parent / "data" / "decisions.json"

def _path() -> Path:
    return Path(os.environ.get("DECISIONS_PATH", DEFAULT_PATH))

def _load() -> List[Dict[str, Any]]:
    try:
        return json.loads(_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def get_history(ticker: str) -> List[Dict[str, Any]]:
    return [d for d in _load() if d["ticker"] == ticker.upper()]

def save_decision(ticker: str, decision: Dict[str, Any], entry_price: Optional[float]) -> None:
    records = _load()
    records.append({
        "ticker": ticker.upper(),
        "date": date.today().isoformat(),
        "entry_price": entry_price,
        "rating": decision.get("rating"),
        "target_price": decision.get("target_price"),
        "stop_loss": decision.get("stop_loss"),
        "justification": decision.get("justification"),
    })
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2))

def get_past_context(ticker: str, current_price: Optional[float]) -> str:
    """One short paragraph about the previous decision and its realised return."""
    history = get_history(ticker)
    if not history:
        return "No previous decisions on this ticker."
    last = history[-1]
    entry = last.get("entry_price")
    line = f"On {last['date']} you rated {ticker.upper()} {last['rating']}"
    if entry and current_price:
        ret = (current_price / entry - 1) * 100
        line += f" at ${entry:.2f}; it is now ${current_price:.2f} ({ret:+.1f}%)."
    else:
        line += "; realised return unavailable."
    if last.get("target_price") or last.get("stop_loss"):
        line += f" Target was {last.get('target_price')}, stop {last.get('stop_loss')}."
    return line + f" Reasoning then: {(last.get('justification') or '')[:400]} Reflect on whether it held up."
