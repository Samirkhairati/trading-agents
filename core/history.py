"""One JSON file per finished analysis (full reports, debate, risk views), so the UI can browse every past run."""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from paths import state_dir

def _dir() -> Path:
    return Path(os.environ.get("ANALYSES_DIR", state_dir() / "analyses"))

def save_analysis(state: Dict[str, Any], stages: List[dict], seconds: float, model: str) -> str:
    now = datetime.now()
    aid = f"{now:%Y%m%d-%H%M%S}-{state['ticker']}"
    _dir().mkdir(parents=True, exist_ok=True)
    rec = {"id": aid, "time": now.isoformat(timespec="seconds"), "seconds": round(seconds), "model": model,
           "stages": [{k: s[k] for k in ("name", "seconds", "output")} for s in stages], "state": state}
    (_dir() / f"{aid}.json").write_text(json.dumps(rec, indent=2, default=str))
    return aid

def list_analyses(ticker: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Newest first, summaries only."""
    out = []
    for p in sorted(_dir().glob("*.json"), reverse=True):
        if ticker and not p.stem.endswith(f"-{ticker.upper()}"):
            continue
        try:
            r = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        st, d = r["state"], r["state"].get("final_decision") or {}
        out.append({"id": r["id"], "time": r["time"], "ticker": st["ticker"], "rating": d.get("rating"), "price": st.get("last_price"),
                    "target": d.get("target_price"), "stop": d.get("stop_loss"), "seconds": r.get("seconds"), "model": r.get("model")})
        if len(out) >= limit:
            break
    return out

def get_analysis(aid: str) -> Optional[Dict[str, Any]]:
    p = _dir() / f"{Path(aid).name}.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def stage_stats() -> Dict[str, Any]:
    """Average seconds per agent stage over the saved analyses (for the flow page)."""
    sums: Dict[str, List[float]] = {}
    totals, runs = [], 0
    for p in _dir().glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        runs += 1
        totals.append(r.get("seconds") or 0)
        for s in r.get("stages", []):
            if s.get("seconds"):
                sums.setdefault(s["name"], []).append(s["seconds"])
    return {"runs": runs, "avg_total": round(sum(totals) / len(totals)) if totals else None,
            "stages": {k: round(sum(v) / len(v)) for k, v in sums.items()}}
