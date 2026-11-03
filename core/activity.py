"""In-memory view of what the pipeline is doing right now. Written by the graph / daily run, read by GET /api/live."""
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_s: Dict[str, Any] = {"running": False, "kind": None, "label": "", "started": None, "log": [], "analysis": None, "last": None}

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")  # with offset: the browser computes elapsed time from it

def busy() -> bool:
    return _s["running"]

def log(text: str) -> None:
    with _lock:
        _s["log"] = (_s["log"] + [{"t": _now(), "text": str(text).strip()}])[-300:]

def job_start(kind: str, label: str) -> bool:
    """Mark a job (daily cycle or single analysis) as running. Returns True if the caller now owns it."""
    with _lock:
        if _s["running"]:
            return False
        _s.update(running=True, kind=kind, label=label, started=_now(), log=[], analysis=None)
        _s["_t0"] = time.monotonic()
    log(f"{kind} started: {label}")
    return True

def job_end(owned: bool, ok: bool, message: str = "") -> None:
    if not owned:
        return
    log(message or ("done" if ok else "failed"))
    with _lock:
        _s["last"] = {"kind": _s["kind"], "label": _s["label"], "started": _s["started"], "ended": _now(),
                      "ok": ok, "message": message, "seconds": round(time.monotonic() - _s.pop("_t0", time.monotonic()))}
        _s.update(running=False, kind=None, label="", started=None, analysis=None)

def analysis_start(ticker: str, stages: List[str]) -> None:
    with _lock:
        _s["analysis"] = {"ticker": ticker, "started": _now(), "_t": time.monotonic(),
                          "stages": [{"name": n, "done": False, "seconds": None, "output": ""} for n in stages]}
    log(f"analysing {ticker}")

def analysis_stage(name: str, output: str = "") -> None:
    """Mark the first unfinished stage called `name` as done."""
    with _lock:
        a = _s["analysis"]
        if not a:
            return
        now = time.monotonic()
        for st in a["stages"]:
            if st["name"] == name and not st["done"]:
                st.update(done=True, seconds=round(now - a["_t"], 1), output=output)
                a["_t"] = now
                break
    log(f"{name} finished")

def analysis_stages() -> List[dict]:
    with _lock:
        a = _s["analysis"]
        return [dict(s) for s in a["stages"]] if a else []

def analysis_end() -> None:
    with _lock:
        _s["analysis"] = None

def snapshot() -> Dict[str, Any]:
    """JSON-safe copy. Stage state: done | running (the first unfinished one, or all unfinished analysts) | pending."""
    with _lock:
        out = {k: v for k, v in _s.items() if not k.startswith("_")}
        a = _s["analysis"]
        if a:
            stages = [dict(s) for s in a["stages"]]
            first = next((i for i, s in enumerate(stages) if not s["done"]), None)
            for i, s in enumerate(stages):
                s["state"] = "done" if s["done"] else "pending"
                if first is not None and not s["done"] and (i == first or (first < 4 and i < 4)):
                    s["state"] = "running"
            out["analysis"] = {"ticker": a["ticker"], "started": a["started"], "stages": stages,
                               "elapsed": round(time.monotonic() - a["_t"] + sum(s["seconds"] or 0 for s in stages))}
        out["log"] = list(_s["log"])
        return out
