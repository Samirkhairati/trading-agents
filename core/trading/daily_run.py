"""The paper-trading loop: screen -> analyse -> plan -> risk gate -> execute -> log.

    python -m trading.daily_run                 # dry run (default): prints what it WOULD do
    python -m trading.daily_run --execute       # sends orders to the paper account
    python -m trading.daily_run --schedule      # every market, weekdays after its close (US 16:30 ET, India 16:00 IST)
    python -m trading.daily_run --market in     # the India portfolio (default: us)
"""
import argparse
import logging
import os
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

import activity
import observability as obs
from trading import markets, state as st
from trading.broker import AlpacaBroker, Broker, FakeBroker
from trading.planner import PortfolioAgent, add_rule_intents, build_context, intents_to_orders
from trading.risk import check_orders, drawdown_pct
from trading.screener import analyst_signals, fetch_market, fetch_universe, pick_with_reasons, score_watchlist
from trading.shorting import fetch_short_facts, short_blockers
from trading.types import Account, Position, ProposedOrder, Rejection, RiskConfig

logger = logging.getLogger(__name__)

def make_broker(market: str = "us") -> Broker:
    """Seeded demo portfolio if DEMO_PORTFOLIO=1 (US only); Alpaca paper if PORTFOLIO_MODE=alpaca (US only, keys needed);
    else the persistent simulated account of that market."""
    if market == "us":
        if os.environ.get("DEMO_PORTFOLIO") == "1":
            from trading.demo import load_broker
            return load_broker()
        if os.environ.get("PORTFOLIO_MODE") == "alpaca":
            return AlpacaBroker()
    from trading.sim import SimBroker
    return SimBroker(market)

def risk_config(market: str = "us") -> RiskConfig:
    """Risk limits for a market: the defaults, the market's minimum order, and ALLOW_SHORTS=0 to switch shorting off."""
    return RiskConfig(min_order_usd=markets.get(market).min_order, allow_shorts=os.environ.get("ALLOW_SHORTS", "1") != "0")

def _vet_shorts(orders: List[ProposedOrder], decisions: Dict[str, dict], broker: Broker, facts_fn: Callable[[str], Dict[str, Any]]) -> Any:
    """Only let a short through when `short_blockers` finds nothing against it. Returns (orders to go on with, rejections)."""
    ok: List[ProposedOrder] = []
    vetoed: List[Rejection] = []
    for o in orders:
        if o.side != "short":
            ok.append(o)
            continue
        shortable = broker.can_short(o.symbol) if hasattr(broker, "can_short") else None
        why = short_blockers(decisions.get(o.symbol, {}).get("rating", ""), facts_fn(o.symbol), shortable)
        if why:
            vetoed.append(Rejection(o, "short_guard: " + "; ".join(why)))
        else:
            ok.append(o)
    return ok, vetoed

def _prices(broker: Broker, symbols: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for s in symbols:
        try:
            if isinstance(broker, FakeBroker) and s not in broker.prices:
                import yfinance as yf
                broker.set_price(s, float(yf.Ticker(s).history(period="5d")["Close"].iloc[-1]))
            out[s] = broker.get_price(s)
        except Exception as e:
            logger.warning("no price for %s: %s", s, e)
    return out

def _run_daily(
    broker: Optional[Broker] = None,
    execute: bool = False,
    tickers: Optional[List[str]] = None,
    n: Optional[int] = None,
    cfg: Optional[RiskConfig] = None,
    analyse: Optional[Callable[[str], Dict[str, Any]]] = None,
    scores: Optional[Dict[str, float]] = None,
    plan_fn: Optional[Callable[[str], Any]] = None,
    log: Callable[[str], None] = print,
    market: str = "us",
    analyst: Optional[Dict[str, float]] = None,
    short_facts: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run one cycle for a market and return the logged record. Everything external is injectable for tests."""
    mk = markets.get(market)
    broker = broker or make_broker(mk.code)
    cfg = cfg or risk_config(mk.code)
    n = n if n is not None else int(os.environ.get("DAILY_CANDIDATES", "3"))
    analysis_ids: Dict[str, str] = {}
    if analyse is None:
        from graph.trading_graph import run_analysis

        def analyse(t: str) -> Dict[str, Any]:
            s = run_analysis(t)
            analysis_ids[t] = s.get("analysis_id")
            return s["final_decision"]
    plan_fn = plan_fn or PortfolioAgent().plan

    state = st.load_state(mk.code)
    if execute and hasattr(broker, "check_exits") and not isinstance(broker, AlpacaBroker):  # the simulated account has no exchange to fire its stops
        for sym, kind in broker.check_exits():
            log(f"  {sym}: {'stop-loss' if kind == 'stop' else 'take-profit'} hit, position closed")
    account, positions = broker.get_account(), broker.get_positions()
    held = [p.symbol for p in positions]
    cur = mk.currency
    log(f"{mk.label} account: equity {cur}{account.equity:,.2f}, cash {cur}{account.cash:,.2f}, {len(positions)} positions ({'EXECUTE' if execute else 'DRY RUN'})")

    # 1-2. screen
    screen: Dict[str, Any] = {}
    if tickers is None:
        if analyst is None:  # analyst picks come from the same cached screens as the movers (nothing is fetched twice)
            analyst = analyst_signals(fetch_market(mk.code)["quotes"]) if scores is None else {}
        scores = scores if scores is not None else score_watchlist(fetch_universe(mk.code))
        picks = pick_with_reasons(
            scores, held, state["last_analysed"], n=n,
            review_days=int(os.environ.get("HELD_REVIEW_DAYS", "7")), max_held=int(os.environ.get("DAILY_HELD_MAX", "4")),
            analyst=analyst, n_buy=int(os.environ.get("DAILY_ANALYST_BUYS", "1")),
            n_sell=int(os.environ.get("DAILY_ANALYST_SELLS", "1")) if cfg.allow_shorts else 0)
        tickers = [t for t, _ in picks]
        screen = {t: {"why": why, "score": round(scores.get(t, 0.0), 2)} for t, why in picks}
    labels = [f"{t} ({screen[t]['why']})" if t in screen else t for t in tickers]
    log(f"Candidates: {', '.join(labels) or '(none)'}")

    # 3. analyse (one failure must not stop the run)
    decisions: Dict[str, dict] = {}
    errors: Dict[str, str] = {}
    for t in tickers:
        try:
            decisions[t] = analyse(t)
            state["last_analysed"][t] = date.today().isoformat()
            log(f"  {t}: {decisions[t]['rating']}")
        except Exception as e:
            errors[t] = (str(e).splitlines() or [type(e).__name__])[0]
            log(f"  {t}: analysis failed ({errors[t]})")

    # 4-6. plan -> orders -> risk gate
    prices = _prices(broker, sorted(set(decisions) | set(held)))
    plan = plan_fn(build_context(account, positions, decisions, _recent_trades(mk.code), cur)) if decisions or positions else None
    if plan:
        plan = add_rule_intents(plan, decisions, positions, cfg)
    orders = intents_to_orders(plan, account, positions, prices) if plan else []
    orders, vetoed = _vet_shorts(orders, decisions, broker, short_facts or fetch_short_facts)
    approved, rejected = check_orders(orders, account, positions, cfg, state["peak_equity"])
    rejected = vetoed + rejected
    obs.ORDERS.labels("approved").inc(len(approved))
    obs.ORDERS.labels("rejected").inc(len(rejected))
    for r in rejected:
        obs.ORDERS_REJECTED.labels(r.reason.split(":")[0].strip() or "unknown").inc()
    for o in approved:
        log(f"  APPROVE {o.side.upper():5} {o.symbol} {cur}{o.notional:,.0f} stop {o.stop_loss} take {o.take_profit} | {o.reason}")
    for r in rejected:
        log(f"  REJECT  {r.order.side.upper():5} {r.order.symbol}: {r.reason}")

    # 7. execute
    fills: List[dict] = []
    if execute:
        for o in approved:
            try:
                fills.append({"symbol": o.symbol, "side": o.side, "order_id": broker.place_order(o)})
                obs.ORDERS.labels("executed").inc()
            except Exception as e:
                obs.ORDERS.labels("failed").inc()
                obs.BROKER_ERRORS.inc()
                fills.append({"symbol": o.symbol, "side": o.side, "error": str(e).splitlines()[0]})
        log(f"Placed {len([f for f in fills if 'order_id' in f])}/{len(approved)} orders")

    # 8. log
    final = broker.get_account()
    st.record_equity(state, final.equity, final.cash)
    st.save_state(state, mk.code)
    if mk.code == "us":  # the Prometheus gauges are USD and unlabelled: they follow the US account
        publish_portfolio(final, broker.get_positions(), state["peak_equity"])
    record = {
        "market": mk.code, "time": datetime.now().astimezone().isoformat(timespec="seconds"), "executed": execute, "equity": final.equity, "cash": final.cash,
        "candidates": tickers, "screen": screen, "stance": _stance(plan, orders, held), "decisions": decisions, "analysis_ids": analysis_ids, "errors": errors, "summary": plan.summary if plan else "",
        "approved": [asdict(o) for o in approved], "rejected": [{**asdict(r.order), "reason": r.reason} for r in rejected], "fills": fills,
    }
    st.log_run(record, mk.code)
    return record

STANCES = ("enter_new", "maintain", "rebalance", "do_nothing")

def _stance(plan: Any, orders: List[Any], held: List[str]) -> Optional[str]:
    """The planner's stance; small models sometimes leave the field empty or put it in the summary, so fall back to the orders."""
    if plan is None:
        return None
    if plan.stance:
        return plan.stance
    if plan.summary.strip() in STANCES:
        return plan.summary.strip()
    buys = [o for o in orders if o.side == "buy"]
    sells = [o for o in orders if o.side == "sell"]
    if not orders:
        return "do_nothing"
    if buys and sells:
        return "rebalance"
    return "enter_new" if any(o.symbol not in held for o in buys) else "maintain"

def publish_portfolio(account: Account, positions: List[Position], peak_equity: float) -> None:
    """Refresh the portfolio gauges Prometheus scrapes."""
    obs.EQUITY.set(account.equity)
    obs.CASH.set(account.cash)
    obs.PEAK.set(peak_equity)
    obs.DRAWDOWN.set(drawdown_pct(account.equity, peak_equity))
    obs.POSITIONS.set(len(positions))

def refresh_gauges() -> None:
    """Every few minutes, for each market: fire the simulated account's stops / take-profits, add today's equity point and
    (US only) update the portfolio gauges."""
    for code in (m.code for m in markets.enabled()):
        try:
            broker = make_broker(code)
            simulated = not isinstance(broker, AlpacaBroker) and os.getenv("DEMO_PORTFOLIO") != "1"
            if simulated and hasattr(broker, "check_exits"):
                for sym, kind in broker.check_exits():
                    logger.info("%s: %s hit, position closed", sym, "stop-loss" if kind == "stop" else "take-profit")
            account = broker.get_account()
            state = st.load_state(code)
            if simulated:
                st.record_equity(state, account.equity, account.cash)  # one point per day, latest wins
                st.save_state(state, code)
            if code == "us":
                publish_portfolio(account, broker.get_positions(), max(state["peak_equity"] or 0.0, account.equity))
        except Exception as e:
            obs.BROKER_ERRORS.inc()
            logger.warning("%s portfolio refresh failed: %s", code, e)

def run_daily(*args, **kwargs) -> Dict[str, Any]:
    """Run one cycle with metrics and a trace around it (see _run_daily for the arguments)."""
    start = time.monotonic()
    label = markets.get(kwargs.get("market", "us")).label
    owned = activity.job_start("daily", f"{label} · {'EXECUTE' if kwargs.get('execute') else 'dry run'}")
    log = kwargs.get("log", print)
    kwargs["log"] = lambda line: (activity.log(line), log(line))[1]
    with obs.span("daily_run", kind="span") as root:
        obs.tag_trace(root, "daily run", execute=bool(kwargs.get("execute")))
        try:
            record = _run_daily(*args, **kwargs)
        except Exception as e:
            obs.DAILY_RUNS.labels("error").inc()
            activity.job_end(owned, False, (str(e).splitlines() or [type(e).__name__])[0])
            raise
        finally:
            obs.DAILY_SECONDS.observe(time.monotonic() - start)
    obs.DAILY_RUNS.labels("ok").inc()
    obs.DAILY_LAST_OK.set_to_current_time()
    activity.job_end(owned, True, f"daily run done, {len(record['approved'])} orders approved")
    return record

def _recent_trades(market: str = "us", k: int = 3) -> str:
    lines = []
    for run in st.get_runs(market)[-k:]:
        for o in run.get("approved", []):
            lines.append(f"{run['time'][:10]} {o['side']} {o['symbol']} ({'executed' if run['executed'] else 'dry run'}): {o['reason'][:120]}")
    return "\n".join(lines)

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="send orders to the paper account (default is dry run)")
    ap.add_argument("--schedule", action="store_true", help="run every weekday after each market's close (US and India)")
    ap.add_argument("--market", default="us", choices=list(markets.MARKETS), help="which market's portfolio to run (default us)")
    ap.add_argument("--tickers", nargs="*", help="analyse exactly these instead of screening")
    ap.add_argument("-n", type=int, default=None, help="new candidates from the live market screens (default $DAILY_CANDIDATES or 3)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    obs.init_tracing()

    if args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        sched = BlockingScheduler()
        for mk in markets.MARKETS.values():
            h, m = markets.daily_at(mk)
            sched.add_job(lambda code=mk.code: run_daily(execute=args.execute, market=code, n=args.n), "cron",
                          day_of_week="mon-fri", hour=h, minute=m, timezone=mk.tz)
            print(f"Scheduled {mk.label}: weekdays {h:02d}:{m:02d} {mk.tz}")
        print("Ctrl+C to stop.")
        sched.start()
    else:
        try:
            run_daily(execute=args.execute, tickers=args.tickers, n=args.n, market=args.market)
        finally:
            obs.shutdown()

if __name__ == "__main__":
    main()
