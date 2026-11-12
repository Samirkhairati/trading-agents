import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from typing import Optional
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
import activity
import admin
import history
import jobs
import memory
import observability as obs
from trading import markets
from pydantic import BaseModel
from typing import Dict, Any

from data.fetcher import fetch_price_history, price_records, fetch_fundamentals, fetch_news
from data.alternative_fetchers import (
    fetch_reddit_posts,
    fetch_stocktwits_messages,
    fetch_fred_macro_data,
    fetch_polymarket_odds
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

sched = None  # BackgroundScheduler, set in lifespan; /api/schedule reads the next fire time from it

ET = ZoneInfo("America/New_York")

def _market(code: str) -> markets.Market:
    try:
        return markets.get(code)
    except ValueError as e:
        raise HTTPException(400, str(e))

def _scheduled_daily(market: str = "us") -> None:
    """Queue a market's daily cycle (it waits behind whatever is running)."""
    try:
        jobs.submit("daily", "schedule", execute=os.environ.get("SCHEDULER_EXECUTE") == "1", market=market)
    except jobs.Refused as r:
        logger.warning("scheduled %s daily run not queued: %s", market, r.message)

def _catch_up() -> None:
    """For each market: weekday, past its scheduled time and no cycle recorded today (server was down or redeployed): queue one now."""
    from trading import state as trading_state
    for mk in markets.enabled():
        tz = ZoneInfo(mk.tz)
        now, (h, m) = datetime.now(tz), markets.daily_at(mk)
        if now.weekday() >= 5 or (now.hour, now.minute) < (h, m):
            continue
        runs = trading_state.get_runs(mk.code)
        if runs and datetime.fromisoformat(runs[-1]["time"]).astimezone(tz).date() == now.date():
            continue
        logger.info("no %s daily run recorded today: queueing one now", mk.label)
        _scheduled_daily(mk.code)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start tracing, a 5-minute portfolio refresh, and (ENABLE_SCHEDULER=1) a weekday daily cycle per market, each after its own close."""
    obs.init_tracing()
    from apscheduler.schedulers.background import BackgroundScheduler
    from trading.daily_run import refresh_gauges

    global sched
    sched = BackgroundScheduler(timezone=ET)
    sched.add_job(refresh_gauges, "interval", minutes=5, id="gauges", next_run_time=datetime.now(ET))
    if os.environ.get("ENABLE_SCHEDULER") == "1":
        for mk in markets.enabled():
            h, m = markets.daily_at(mk)
            sched.add_job(_scheduled_daily, "cron", args=[mk.code], day_of_week=os.environ.get("DAILY_DAYS", "mon-fri"), hour=h, minute=m,
                          timezone=mk.tz, id=f"daily-{mk.code}", misfire_grace_time=3600)
            logger.info("scheduler on: %s daily cycle %s at %02d:%02d %s (%s)", mk.label, os.environ.get("DAILY_DAYS", "mon-fri"), h, m, mk.tz,
                        "EXECUTE" if os.environ.get("SCHEDULER_EXECUTE") == "1" else "dry run")
        sched.add_job(_catch_up, "date", run_date=datetime.now(ET) + timedelta(seconds=20), id="catchup")
    sched.start()
    yield
    sched.shutdown(wait=False)
    obs.shutdown()

app = FastAPI(title="Trading Agents API", lifespan=lifespan)
app.include_router(admin.router)

@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint."""
    body, content_type = obs.render_metrics()
    return Response(body, media_type=content_type)

# ----------------------------------------------------------------------
# MICRO-ENDPOINTS FOR CONCURRENT LOADING (Phase 4)
# ----------------------------------------------------------------------

executor = ThreadPoolExecutor(max_workers=10)

def _finite(x):
    """NaN / inf are not valid JSON (yfinance returns them); turn them into null."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x

@app.get("/api/data/fundamentals/{ticker}")
async def api_fundamentals(ticker: str):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, fetch_fundamentals, ticker)
        return {"ticker": ticker.upper(), "data": _finite(data)}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}

@app.get("/api/data/technicals/{ticker}")
async def api_technicals(ticker: str, days: int = 30):
    loop = asyncio.get_running_loop()
    try:
        df = await loop.run_in_executor(executor, fetch_price_history, ticker, "1y")
        data = price_records(df, days)
        return {"ticker": ticker.upper(), "data": _finite(data)}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}

@app.get("/api/data/news/{ticker}")
async def api_news(ticker: str):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, fetch_news, ticker)
        return {"ticker": ticker.upper(), "data": _finite(data)}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}

def _gather_sentiment(ticker: str):
    reddit = fetch_reddit_posts(ticker)
    stocktwits = fetch_stocktwits_messages(ticker)
    return {"reddit": reddit, "stocktwits": stocktwits}

@app.get("/api/data/sentiment/{ticker}")
async def api_sentiment(ticker: str):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, _gather_sentiment, ticker)
        return {"ticker": ticker.upper(), "data": _finite(data)}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}

def _gather_macro():
    fred = fetch_fred_macro_data()
    polymarket = fetch_polymarket_odds("interest rates")
    return {"fred": fred, "polymarket": polymarket}

@app.get("/api/data/macro")
async def api_macro():
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, _gather_macro)
        return {"data": data}
    except Exception as e:
        return {"error": str(e)}

# ----------------------------------------------------------------------
# ANALYSES: visitors can queue one (see jobs.py); everything else here is read-only
# ----------------------------------------------------------------------

@app.get("/api/history/{ticker}")
async def get_ticker_history(ticker: str):
    return {"ticker": ticker.upper(), "history": memory.get_history(ticker)}

# ----------------------------------------------------------------------
# PAPER-TRADING PORTFOLIO
# ----------------------------------------------------------------------

@app.get("/api/portfolio")
async def get_portfolio(market: str = "us"):
    from trading.view import portfolio_snapshot

    mk = _market(market)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, portfolio_snapshot, mk.code)
    except Exception as e:
        raise HTTPException(502, (str(e).splitlines() or [type(e).__name__])[0])

@app.get("/api/trades")
async def get_trades(limit: int = 20, market: str = "us"):
    from trading import state as trading_state
    return {"runs": list(reversed(trading_state.get_runs(_market(market).code)))[:limit]}

# ----------------------------------------------------------------------
# WEB UI DATA: live activity, schedule, past analyses
# ----------------------------------------------------------------------

@app.get("/api/live")
async def get_live():
    return {**activity.snapshot(), "queue": jobs.snapshot()}

@app.get("/api/config")
async def get_config():
    model = os.environ.get("LLM_MODEL", "gemini/gemini-3.5-flash")
    return {"model": model, "deep_model": os.environ.get("LLM_DEEP_MODEL") or model,
            "local": model.startswith(("ollama", "openai/")), "debate_rounds": int(os.environ.get("DEBATE_ROUNDS", "2")),
            "demo": os.environ.get("DEMO_PORTFOLIO") == "1", "markets": [markets.public(m) for m in markets.enabled()],
            "shorting": os.environ.get("ALLOW_SHORTS", "1") != "0"}

@app.get("/api/schedule")
async def get_schedule(market: str = "us"):
    mk = _market(market)
    job = sched.get_job(f"daily-{mk.code}") if sched else None
    h, m = markets.daily_at(mk)
    return {"enabled": job is not None, "execute": os.environ.get("SCHEDULER_EXECUTE") == "1", "rule": f"{os.environ.get('DAILY_DAYS', 'mon-fri')} {h:02d}:{m:02d} {mk.tz}",
            "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None, "now": datetime.now().astimezone().isoformat()}

@app.get("/api/watchlist")
async def get_watchlist(market: str = "us"):
    from trading.screener import fetch_universe
    return {"tickers": await run_in_threadpool(fetch_universe, _market(market).code)}

@app.get("/api/analyses")
async def list_analyses(ticker: Optional[str] = None, limit: int = 100, market: Optional[str] = None):
    return {"analyses": history.list_analyses(ticker, limit, _market(market).code if market else None)}

@app.get("/api/stats")
async def get_stats():
    return history.stage_stats()

@app.get("/api/analyses/{analysis_id}")
async def get_analysis(analysis_id: str):
    rec = history.get_analysis(analysis_id)
    if rec is None:
        raise HTTPException(404, "unknown analysis")
    return rec

_hits: Dict[str, list] = {}

class QueueRequest(BaseModel):
    ticker: str

@app.post("/api/queue")
async def queue_analysis(req: QueueRequest, request: Request):
    """Visitors can queue a single-stock analysis. At most QUEUE_MAX wait at once; one waiting analysis per visitor."""
    ip, now = admin.client_ip(request), time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < 60]
    _hits[ip] = recent + [now]
    snap = jobs.snapshot()
    if len(recent) >= 6:
        return JSONResponse({"error": "Too many requests, slow down.", "queue": snap}, status_code=429)
    ticker = req.ticker.strip().upper()
    mk = markets.get(markets.market_of(ticker))
    pattern = r"[A-Z]{1,5}([.-][A-Z])?" if mk.code == "us" else r"[A-Z0-9&-]{1,20}\.NS"
    if not re.fullmatch(pattern, ticker):
        return JSONResponse({"error": "That does not look like a stock ticker (US: AAPL, India: RELIANCE.NS).", "queue": snap}, status_code=400)
    if snap["lock"]:  # the daily cycle owns the model
        return JSONResponse({"error": "No: " + snap["lock"]["message"], "queue": snap}, status_code=423)
    if snap["waiting"] >= jobs.QUEUE_MAX:  # cheap early refusal before the price lookup
        return JSONResponse({"error": f"No: the queue is full ({jobs.QUEUE_MAX}/{jobs.QUEUE_MAX}). Please wait for an analysis to finish.", "queue": snap}, status_code=429)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(executor, fetch_price_history, ticker)
    except Exception:
        return JSONResponse({"error": f"No recent price data for {ticker}; is that a listed {mk.label} stock?", "queue": snap}, status_code=400)
    try:
        return jobs.submit("analysis", "user", ticker=ticker, ip=ip)
    except jobs.Refused as r:
        return JSONResponse({"error": r.message, "queue": r.queue}, status_code=r.status)

@app.get("/admin", include_in_schema=False)
async def admin_page():
    """The admin login page. Nothing in the public UI links here."""
    return FileResponse(Path(__file__).parent / "web" / "admin.html", headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"})

# The web UI (plain HTML/JS, no build step). Mounted last so /api and /metrics win.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
