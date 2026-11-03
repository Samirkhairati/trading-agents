"""One worker thread runs pipeline jobs one at a time (the model is a single CPU worker).
Visitors may queue analyses, but at most QUEUE_MAX can be waiting at once; the scheduler and admin jobs bypass that cap."""
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, Optional

import activity

logger = logging.getLogger(__name__)
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "3"))

class Refused(Exception):
    """A queue request that was turned down; `message` is shown to the visitor, `queue` is the current state."""
    def __init__(self, message: str, queue: dict, status: int = 429):
        super().__init__(message)
        self.message, self.queue, self.status = message, queue, status

_lock = threading.Condition()
_waiting: Deque[Dict[str, Any]] = deque()
_current: Optional[Dict[str, Any]] = None
_worker: Optional[threading.Thread] = None
_durations: Deque[float] = deque(maxlen=5)  # seconds of recent single analyses, for wait estimates

def _label(j: dict) -> str:
    return j["ticker"] if j["kind"] == "analysis" else f"daily cycle ({'execute' if j['execute'] else 'dry run'})"

def _view(j: dict, pos: int, running: bool) -> dict:
    return {"id": j["id"], "label": _label(j), "kind": j["kind"], "source": j["source"], "position": pos, "running": running,
            "since": j["started"] if running else j["queued"]}

def avg_analysis_seconds() -> int:
    return int(sum(_durations) / len(_durations)) if _durations else 1500

def snapshot() -> Dict[str, Any]:
    with _lock:
        items = ([_view(_current, 0, True)] if _current else []) + [_view(j, i + 1, False) for i, j in enumerate(_waiting)]
        return {"max": QUEUE_MAX, "waiting": len(_waiting), "items": items, "avg_analysis_seconds": avg_analysis_seconds()}

def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")

def submit(kind: str, source: str, ticker: Optional[str] = None, execute: bool = False, ip: Optional[str] = None) -> Dict[str, Any]:
    """Queue a job. kind: analysis | daily. source: user | admin | schedule. Raises Refused for visitors who hit a limit."""
    global _worker
    with _lock:
        if kind == "analysis":
            mine = [j for j in ([_current] if _current else []) + list(_waiting) if j["kind"] == "analysis" and j["ticker"] == ticker]
            if mine:
                raise Refused(f"{ticker} is already running or queued.", snapshot(), 409)
        else:
            if any(j["kind"] == "daily" for j in ([_current] if _current else []) + list(_waiting)):
                raise Refused("A daily cycle is already running or queued.", snapshot(), 409)
        if source == "user":
            if len(_waiting) >= QUEUE_MAX:
                raise Refused(f"No: the queue is full ({QUEUE_MAX}/{QUEUE_MAX}). Please wait for an analysis to finish.", snapshot())
            if ip and any(j["ip"] == ip for j in _waiting):
                raise Refused("You already have an analysis waiting. One at a time per visitor, please.", snapshot())
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "source": source, "ticker": ticker, "execute": execute, "ip": ip,
               "queued": _iso(), "started": None}
        _waiting.append(job)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="job-worker", daemon=True)
            _worker.start()
        _lock.notify()
        return {"id": job["id"], "position": len(_waiting) + (1 if _current else 0), "queue": snapshot()}

def cancel(job_id: str) -> bool:
    with _lock:
        for j in list(_waiting):
            if j["id"] == job_id:
                _waiting.remove(j)
                return True
    return False

def _run() -> None:
    global _current
    while True:
        with _lock:
            while not _waiting:
                _lock.wait()
            job = _waiting.popleft()
            job["started"] = _iso()
            _current = job
        t0 = time.monotonic()
        before = activity.snapshot()["last"]
        try:
            if job["kind"] == "analysis":
                from graph.trading_graph import run_analysis
                run_analysis(job["ticker"])
                _durations.append(time.monotonic() - t0)
            else:
                from trading.daily_run import run_daily
                run_daily(execute=job["execute"])
        except Exception as e:  # the failure is already in activity.last / the logs
            logger.warning("job %s (%s) failed: %s", job["id"], _label(job), (str(e).splitlines() or [type(e).__name__])[0])
            if activity.snapshot()["last"] == before and not activity.busy():  # failed before the pipeline reported anything (e.g. unknown ticker)
                activity.job_start(job["kind"], _label(job))
                activity.job_end(True, False, (str(e).splitlines() or [type(e).__name__])[0])
        finally:
            with _lock:
                _current = None
