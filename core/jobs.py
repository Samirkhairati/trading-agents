"""One worker thread runs pipeline jobs one at a time (the model is a single CPU worker).
Visitors may queue analyses, but at most QUEUE_MAX can be waiting at once; the scheduler and admin jobs bypass that cap."""
import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Optional
from zoneinfo import ZoneInfo

import activity

logger = logging.getLogger(__name__)
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "3"))
LOCK_MINUTES = int(os.environ.get("DAILY_LOCK_MINUTES", "60"))  # visitors cannot queue this long before the daily cycle
ET = ZoneInfo("America/New_York")

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

def _next_daily(now: datetime) -> Optional[datetime]:
    """Next scheduled daily-cycle time (None when the scheduler is off). A minute of grace keeps the window open while the cycle is being queued."""
    if os.environ.get("ENABLE_SCHEDULER") != "1":
        return None
    from apscheduler.triggers.cron import CronTrigger
    h, m = os.environ.get("DAILY_AT", "16:30").split(":")
    trigger = CronTrigger(day_of_week=os.environ.get("DAILY_DAYS", "mon-fri"), hour=int(h), minute=int(m), timezone=ET)
    return trigger.get_next_fire_time(None, now - timedelta(seconds=60))

def _in_window(now: datetime) -> Optional[datetime]:
    """The daily cycle's start time when it is within LOCK_MINUTES (or just passed), else None."""
    nxt = _next_daily(now)
    return nxt if nxt and nxt - now <= timedelta(minutes=LOCK_MINUTES) else None

def lock(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Why visitors cannot queue an analysis right now (the daily cycle owns the model), or None."""
    now = now or datetime.now(ET)
    with _lock:
        if any(j["kind"] == "daily" for j in ([_current] if _current else []) + list(_waiting)):
            return {"kind": "daily", "message": "The daily cycle is running (it screens stocks, analyses them and trades the account). "
                                                "Analyses open again when it finishes."}
    start = _in_window(now)
    if start:
        mins = max(0, int((start - now).total_seconds() // 60))
        return {"kind": "window", "starts": start.isoformat(),
                "message": f"The daily cycle starts at {start.astimezone(ET):%H:%M} ET ({'in ' + str(mins) + ' min' if mins else 'now'}). "
                           f"Analyses are closed for the {LOCK_MINUTES} minutes before it so the model is free; they open again after the cycle."}
    return None

def _held(j: dict, now: datetime) -> bool:
    """A visitor's waiting analysis does not start while the daily cycle is due or queued: the cycle goes first."""
    return j["source"] == "user" and (_in_window(now) is not None or any(w["kind"] == "daily" for w in _waiting))

def _label(j: dict) -> str:
    return j["ticker"] if j["kind"] == "analysis" else f"daily cycle ({'execute' if j['execute'] else 'dry run'})"

def _view(j: dict, pos: int, running: bool, held: bool = False) -> dict:
    return {"id": j["id"], "label": _label(j), "kind": j["kind"], "source": j["source"], "position": pos, "running": running,
            "held": held, "since": j["started"] if running else j["queued"]}

def avg_analysis_seconds() -> int:
    return int(sum(_durations) / len(_durations)) if _durations else 1500

def snapshot() -> Dict[str, Any]:
    now = datetime.now(ET)
    with _lock:
        items = ([_view(_current, 0, True)] if _current else []) + [_view(j, i + 1, False, _held(j, now)) for i, j in enumerate(_waiting)]
        return {"max": QUEUE_MAX, "waiting": len(_waiting), "items": items, "avg_analysis_seconds": avg_analysis_seconds(), "lock": lock(now)}

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
            locked = lock()
            if locked:
                raise Refused("No: " + locked["message"], snapshot(), 423)
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

def _next_job() -> Optional[Dict[str, Any]]:
    """Pop the first job that may start (caller holds the lock)."""
    now = datetime.now(ET)
    for j in _waiting:
        if not _held(j, now):
            _waiting.remove(j)
            return j
    return None

def _run() -> None:
    global _current
    while True:
        with _lock:
            job = _next_job()
            while job is None:
                _lock.wait(30 if _waiting else None)  # held jobs are rechecked every 30 s (the window opens and closes with the clock)
                job = _next_job()
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
