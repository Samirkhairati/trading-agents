"""Password-protected admin API (/api/admin/*): add cash, sell positions, run the daily cycle. The public UI never links to it.
ADMIN_PASSWORD comes from the environment; without it the admin API is disabled."""
import hmac
import os
import secrets
import time
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import jobs

router = APIRouter(prefix="/api/admin")
TTL = 12 * 3600
_sessions: Dict[str, float] = {}  # session token -> expiry
_fails: Dict[str, List[float]] = {}  # ip -> recent failed logins

def client_ip(request: Request) -> str:
    """The visitor's address. X-Forwarded-For (set by Caddy) is only trusted when the connection comes from the proxy."""
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "")
    if xff and peer.startswith(("127.", "10.", "172.", "192.168.", "::1")):
        return xff.split(",")[0].strip()
    return peer

def require(request: Request) -> None:
    if _sessions.get(request.cookies.get("admin_session") or "", 0) < time.time():
        raise HTTPException(401, "login required")

class Login(BaseModel):
    password: str

@router.post("/login")
def login(body: Login, request: Request, response: Response):
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "admin is not configured")
    ip, now = client_ip(request), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "too many attempts, try again in a few minutes")
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        _fails[ip] = recent + [now]
        time.sleep(1)
        raise HTTPException(401, "wrong password")
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + TTL
    response.set_cookie("admin_session", token, httponly=True, samesite="strict", path="/", max_age=TTL,
                        secure=request.headers.get("x-forwarded-proto") == "https")
    return {"ok": True}

@router.post("/logout")
def logout(request: Request, response: Response):
    _sessions.pop(request.cookies.get("admin_session") or "", None)
    response.delete_cookie("admin_session", path="/")
    return {"ok": True}

@router.get("/overview", dependencies=[Depends(require)])
def overview():
    from trading.view import portfolio_snapshot
    return {"portfolio": portfolio_snapshot(), "queue": jobs.snapshot()}

def _snapshot_equity(broker) -> None:
    from trading import state as st
    s = st.load_state()
    a = broker.get_account()
    st.record_equity(s, a.equity, a.cash)
    st.save_state(s)

class Cash(BaseModel):
    amount: float = Field(gt=0, le=10_000_000)

@router.post("/cash", dependencies=[Depends(require)])
def add_cash(body: Cash):
    from trading.daily_run import make_broker
    broker = make_broker()
    if not hasattr(broker, "deposit"):
        raise HTTPException(400, "this account type cannot take deposits (only the simulated account can)")
    broker.deposit(body.amount)
    _snapshot_equity(broker)
    return {"ok": True, "cash": broker.get_account().cash}

class Sell(BaseModel):
    symbol: str  # a ticker, or ALL

@router.post("/sell", dependencies=[Depends(require)])
def sell(body: Sell):
    from trading.daily_run import make_broker
    broker = make_broker()
    held = [p.symbol for p in broker.get_positions()]
    targets = held if body.symbol.upper() == "ALL" else [body.symbol.upper()]
    if not targets or any(t not in held for t in targets):
        raise HTTPException(400, f"not held: {body.symbol}")
    for t in targets:
        broker.close_position(t, source="admin") if hasattr(broker, "deposit") else broker.close_position(t)
    _snapshot_equity(broker)
    return {"ok": True, "sold": targets}

class Daily(BaseModel):
    execute: bool = False

@router.post("/daily", dependencies=[Depends(require)])
def run_daily(body: Daily):
    try:
        return jobs.submit("daily", "admin", execute=body.execute)
    except jobs.Refused as r:
        raise HTTPException(r.status, r.message)

class Cancel(BaseModel):
    id: str

@router.post("/cancel", dependencies=[Depends(require)])
def cancel(body: Cancel):
    return {"ok": jobs.cancel(body.id)}
