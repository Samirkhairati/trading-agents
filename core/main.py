import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
import uuid
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException

load_dotenv()
import memory
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

app = FastAPI(title="Trading Agents API - Raw Data Explorer")

# ----------------------------------------------------------------------
# MICRO-ENDPOINTS FOR CONCURRENT LOADING (Phase 4)
# ----------------------------------------------------------------------

executor = ThreadPoolExecutor(max_workers=10)

@app.get("/api/data/fundamentals/{ticker}")
async def api_fundamentals(ticker: str):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, fetch_fundamentals, ticker)
        return {"ticker": ticker.upper(), "data": data}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}

@app.get("/api/data/technicals/{ticker}")
async def api_technicals(ticker: str):
    loop = asyncio.get_running_loop()
    try:
        df = await loop.run_in_executor(executor, fetch_price_history, ticker, "1y")
        data = price_records(df)
        return {"ticker": ticker.upper(), "data": data}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}

@app.get("/api/data/news/{ticker}")
async def api_news(ticker: str):
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, fetch_news, ticker)
        return {"ticker": ticker.upper(), "data": data}
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
        return {"ticker": ticker.upper(), "data": data}
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
# ANALYSIS JOBS
# ----------------------------------------------------------------------

jobs: Dict[str, Dict[str, Any]] = {}

class AnalysisRequest(BaseModel):
    ticker: str
    date: Optional[str] = None

def _run_job(job_id: str, ticker: str, date: Optional[str]):
    from graph.trading_graph import run_analysis, TOTAL_STAGES

    job = jobs[job_id]

    def progress(stage: str, done: int, total: int):
        job.update(stage=stage, done=done, total=total, message=f"Finished {stage}")

    try:
        job["message"] = "Fetching data and running analysts..."
        job["total"] = TOTAL_STAGES
        job["result"] = run_analysis(ticker, date, on_progress=progress)
        job.update(state="done", message="Analysis complete")
    except Exception as e:
        logger.exception("job %s failed", job_id)
        job.update(state="error", message=(str(e).splitlines() or [type(e).__name__])[0])

@app.post("/api/analyze")
async def start_analysis(req: AnalysisRequest, background_tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"state": "running", "stage": "Starting", "done": 0, "total": 0, "message": "Starting up...", "result": None}
    background_tasks.add_task(_run_job, job_id, req.ticker, req.date)
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {k: v for k, v in job.items() if k != "result"}

@app.get("/api/result/{job_id}")
async def get_result(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job["state"] != "done":
        raise HTTPException(409, f"job is {job['state']}")
    return job["result"]

@app.get("/api/history/{ticker}")
async def get_ticker_history(ticker: str):
    return {"ticker": ticker.upper(), "history": memory.get_history(ticker)}

# ----------------------------------------------------------------------
# PAPER-TRADING PORTFOLIO
# ----------------------------------------------------------------------

@app.get("/api/portfolio")
async def get_portfolio():
    from trading import state as trading_state
    from trading.broker import AlpacaBroker
    from trading.daily_run import make_broker
    from trading.risk import drawdown_pct

    def build():
        broker = make_broker()
        account, positions = broker.get_account(), broker.get_positions()
        peak = max(trading_state.load_state()["peak_equity"] or 0.0, account.equity)
        return {
            "mode": "alpaca-paper" if isinstance(broker, AlpacaBroker) else "fake (no Alpaca keys)",
            "equity": account.equity, "cash": account.cash, "peak_equity": peak,
            "drawdown_pct": drawdown_pct(account.equity, peak),
            "positions": [
                {"symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry, "price": p.price,
                 "market_value": p.market_value, "pnl_pct": p.pnl_pct} for p in positions
            ],
        }

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, build)
    except Exception as e:
        raise HTTPException(502, (str(e).splitlines() or [type(e).__name__])[0])

@app.get("/api/trades")
async def get_trades(limit: int = 20):
    from trading import state as trading_state
    return {"runs": list(reversed(trading_state.get_runs()))[:limit]}

def _run_daily_job(job_id: str, execute: bool):
    from trading.daily_run import run_daily

    job = jobs[job_id]
    try:
        job["message"] = "Running daily cycle (screen, analyse, plan, risk gate)..."
        job["result"] = run_daily(execute=execute, log=lambda line: job.update(message=line.strip()))
        job.update(state="done", message="Daily run complete")
    except Exception as e:
        logger.exception("daily run %s failed", job_id)
        job.update(state="error", message=(str(e).splitlines() or [type(e).__name__])[0])

@app.post("/api/daily-run")
async def start_daily_run(background_tasks: BackgroundTasks, execute: bool = False):
    """Starts a daily cycle (dry run unless execute=true). Poll /api/status/{job_id}, fetch /api/result/{job_id}."""
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"state": "running", "stage": "Daily run", "done": 0, "total": 0, "message": "Starting...", "result": None}
    background_tasks.add_task(_run_daily_job, job_id, execute)
    return {"job_id": job_id}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
