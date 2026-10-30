from datetime import date

import pytest

from agents.schemas import PlanIntent, PortfolioPlan
from trading import state as st
from trading.broker import FakeBroker
from trading.daily_run import run_daily
from trading.planner import intents_to_orders
from trading.screener import pick_candidates
from trading.types import Account, Position

@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))

DECISION = {"rating": "Buy", "target_price": 120.0, "stop_loss": 90.0, "position_size": 5.0, "justification": "x"}

def test_screener_held_first_cooldown_and_topn():
    today = date(2026, 1, 10)
    scores = {"A": 5, "B": 9, "C": 1, "D": 7}
    picks = pick_candidates(scores, held=["C"], last_analysed={"B": "2026-01-09"}, n=2, cooldown_days=3, today=today)
    assert picks == ["C", "D", "A"]  # held first, B on cooldown, top 2 of the rest

def test_intents_to_orders_sizes_and_defaults():
    acct = Account(cash=50_000, equity=100_000)
    held = [Position("OLD", 10, 50.0, 60.0)]
    plan = PortfolioPlan(summary="s", intents=[
        PlanIntent(symbol="aapl", action="buy", target_weight_pct=8, reason="r"),
        PlanIntent(symbol="OLD", action="sell", target_weight_pct=0, reason="r"),
        PlanIntent(symbol="ZZZ", action="buy", target_weight_pct=5, reason="no price"),
    ])
    orders = intents_to_orders(plan, acct, held, {"AAPL": 100.0})
    assert [o.side for o in orders] == ["sell", "buy"]
    buy = orders[1]
    assert buy.notional == 8000 and buy.stop_loss < 100 < buy.take_profit

def _plan_buy(symbol, weight):
    return lambda ctx: PortfolioPlan(summary="s", intents=[
        PlanIntent(symbol=symbol, action="buy", target_weight_pct=weight, reason="because")])

def test_dry_run_places_nothing_but_logs():
    broker = FakeBroker(cash=100_000, prices={"AAA": 100.0})
    rec = run_daily(broker, tickers=["AAA"], analyse=lambda t: DECISION, plan_fn=_plan_buy("AAA", 8), log=lambda *_: None)
    assert len(rec["approved"]) == 1 and rec["fills"] == []
    assert broker.get_positions() == []
    assert len(st.get_runs()) == 1 and st.load_state()["last_analysed"]["AAA"] == date.today().isoformat()

def test_execute_buys_and_risk_gate_resizes_oversized_order():
    broker = FakeBroker(cash=100_000, prices={"AAA": 100.0})
    rec = run_daily(broker, execute=True, tickers=["AAA"], analyse=lambda t: DECISION, plan_fn=_plan_buy("AAA", 50), log=lambda *_: None)
    assert rec["fills"] and "order_id" in rec["fills"][0]
    pos = broker.get_positions()[0]
    assert pos.market_value <= 10_000 + 1  # capped at max_position_pct

def test_analysis_failure_is_isolated():
    broker = FakeBroker(cash=100_000, prices={"AAA": 100.0, "BBB": 50.0})

    def analyse(t):
        if t == "BBB":
            raise RuntimeError("boom")
        return DECISION

    rec = run_daily(broker, tickers=["AAA", "BBB"], analyse=analyse, plan_fn=_plan_buy("AAA", 5), log=lambda *_: None)
    assert "BBB" in rec["errors"] and rec["approved"]

def test_portfolio_and_trades_api(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import trading.daily_run as dr

    monkeypatch.setattr(dr, "make_broker", lambda: FakeBroker(cash=1000, prices={}))
    c = TestClient(main.app)
    p = c.get("/api/portfolio").json()
    assert p["equity"] == 1000 and p["positions"] == [] and p["drawdown_pct"] == 0
    assert c.get("/api/trades").json() == {"runs": []}

def test_equity_stats():
    from trading.report import equity_stats
    s = equity_stats([{"equity": 100}, {"equity": 120}, {"equity": 90}, {"equity": 110}])
    assert round(s["return_pct"], 1) == 10.0 and round(s["max_drawdown_pct"], 1) == 25.0
