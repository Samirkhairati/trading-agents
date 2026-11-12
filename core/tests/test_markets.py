"""Two markets side by side (US, India) and the analyst-aware screener. No network."""
from datetime import date

import pytest

from agents.schemas import PlanIntent, PortfolioPlan
from trading import markets, screener, state as st
from trading.broker import FakeBroker
from trading.daily_run import risk_config, run_daily

@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    screener._cache.clear()

def test_market_of_and_lookup():
    assert markets.market_of("RELIANCE.NS") == "in" and markets.market_of("aapl") == "us" and markets.market_of("BRK-B") == "us"
    assert markets.get("IN").flag == "🇮🇳" and markets.get(None).code == "us"
    with pytest.raises(ValueError, match="unknown market"):
        markets.get("xx")

def test_enabled_markets_follow_the_env(monkeypatch):
    monkeypatch.setenv("MARKETS", "us,in")
    assert [m.code for m in markets.enabled()] == ["us", "in"]
    monkeypatch.setenv("MARKETS", "in,bogus")
    assert [m.code for m in markets.enabled()] == ["in"]
    monkeypatch.setenv("MARKETS", "bogus")
    assert [m.code for m in markets.enabled()] == ["us"]

def test_schedule_times_are_per_market(monkeypatch):
    assert markets.daily_at(markets.get("us")) == (16, 30) and markets.daily_at(markets.get("in")) == (16, 0)
    monkeypatch.setenv("DAILY_AT_IN", "17:45")
    assert markets.daily_at(markets.get("in")) == (17, 45)

def test_each_market_keeps_its_own_files(tmp_path):
    st.log_run({"time": "t", "n": "us"}, "us")
    st.log_run({"time": "t", "n": "in"}, "in")
    assert (tmp_path / "trades.json").exists() and (tmp_path / "in" / "trades.json").exists()  # US keeps the old location
    assert st.get_runs("us")[0]["n"] == "us" and st.get_runs("in")[0]["n"] == "in"
    s = st.load_state("in")
    s["last_analysed"]["TCS.NS"] = "2026-01-01"
    st.save_state(s, "in")
    assert st.load_state("us")["last_analysed"] == {} and st.load_state("in")["last_analysed"] == {"TCS.NS": "2026-01-01"}

def test_risk_config_uses_the_market_minimum_order(monkeypatch):
    assert risk_config("us").min_order_usd == 50 and risk_config("in").min_order_usd == 5_000
    monkeypatch.setenv("ALLOW_SHORTS", "0")
    assert risk_config("in").allow_shorts is False

DECISION = {"rating": "Buy", "target_price": 1500.0, "stop_loss": 1200.0, "position_size": 5.0, "justification": "x"}

def test_india_cycle_trades_its_own_account_in_rupees():
    broker = FakeBroker(cash=10_000_000, prices={"TCS.NS": 1_000.0})
    lines = []
    plan = lambda ctx: PortfolioPlan(summary="s", intents=[PlanIntent(symbol="TCS.NS", action="buy", target_weight_pct=8, reason="r")])
    rec = run_daily(broker, execute=True, tickers=["TCS.NS"], analyse=lambda t: DECISION, plan_fn=plan, market="in", log=lines.append)
    assert rec["market"] == "in" and rec["approved"][0]["notional"] == pytest.approx(800_000)
    assert any("India account" in l and "₹" in l for l in lines)
    assert len(st.get_runs("in")) == 1 and st.get_runs("us") == []
    assert st.load_state("in")["last_analysed"]["TCS.NS"] == date.today().isoformat() and st.load_state("us")["last_analysed"] == {}

# ---------------------------------------------------------------- screener

def test_analyst_signals_are_signed_and_skip_unrated():
    quotes = {"A": {"analyst": 1.5}, "B": {"analyst": 3.5}, "C": {"analyst": None}, "D": {}}
    assert screener.analyst_signals(quotes) == {"A": 1.5, "B": -0.5}

def test_picks_mix_held_analyst_buys_analyst_sells_and_movers_without_duplicates():
    analyst = {"BUY1": 1.8, "BUY2": 1.2, "WEAK": 0.5, "SELL1": -0.6, "SELL2": -0.3, "OKAY": 0.0, "HELD": 1.9}
    scores = {"MOVER": 9.0, "BUY2": 8.0, "M2": 1.0}
    picks = screener.pick_with_reasons(scores, held=["HELD"], last_analysed={}, n=2, analyst=analyst, n_buy=1, n_sell=1, today=date(2026, 1, 10))
    assert [t for t, _ in picks] == ["HELD", "BUY1", "SELL1", "MOVER", "BUY2"]
    why = dict(picks)
    assert why["HELD"] == "held, due for review" and why["BUY1"] == "analyst recommended buy (1.2/5)"
    assert why["SELL1"] == "analyst recommended sell (3.6/5)" and why["MOVER"] == "top mover / volume spike"

def test_analyst_picks_respect_thresholds_and_cooldown():
    analyst = {"LUKEWARM": 0.9, "SELLISH": -0.1, "BUY": 1.5, "SELL": -0.5}  # needs consensus <= 2.0 / >= 3.2
    last = {"BUY": "2026-01-09"}
    picks = screener.pick_candidates({}, [], last, n=0, analyst=analyst, n_buy=5, n_sell=5, today=date(2026, 1, 10))
    assert picks == ["SELL"]  # BUY is on cooldown, the rest are not convincing

def test_default_picks_are_unchanged_without_analyst_options():
    assert screener.pick_candidates({"A": 5, "B": 9}, held=[], last_analysed={}, n=1) == ["B"]

def _quote(sym, cap=5e10, price=100, rating="2.1 - Buy", **kw):
    return {"symbol": sym, "quoteType": "EQUITY", "marketCap": cap, "regularMarketPrice": price, "averageAnalystRating": rating, **kw}

def test_us_pool_keeps_analyst_ratings_and_drops_small_or_foreign_listings(monkeypatch):
    def fake(name, count=25, **kw):
        if name == "most_actives":
            return {"quotes": [_quote("AAA"), _quote("TINY", cap=1e9), _quote("RELIANCE.NS"), _quote("PENNY", price=2)]}
        if not isinstance(name, str):  # the large-cap pool is an EquityQuery
            return {"quotes": [_quote("BIG", rating="1.4 - Strong Buy", fiftyDayAverage=90.0), _quote("DUD", rating="3.8 - Underperform")]}
        raise RuntimeError("screen down")
    monkeypatch.setattr(screener.yf, "screen", fake)
    pool = screener.fetch_market("us")
    assert pool["universe"] == ["AAA"]  # the pool names are not movers
    assert set(pool["quotes"]) == {"AAA", "BIG", "DUD"}
    assert pool["quotes"]["BIG"]["analyst"] == 1.4 and pool["quotes"]["BIG"]["sma50"] == 90.0
    sig = screener.analyst_signals(pool["quotes"])
    assert sig["BIG"] == 1.6 and sig["DUD"] == pytest.approx(-0.8)

def test_india_screens_use_equity_queries_and_keep_only_nse_names(monkeypatch):
    seen = []
    def fake(query, **kw):
        seen.append((query, kw))
        return {"quotes": [_quote("TCS.NS", cap=1e13, price=3500), _quote("AAPL", cap=1e13), _quote("TINY.NS", cap=1e9), _quote("CHEAP.NS", cap=1e12, price=5)]}
    monkeypatch.setattr(screener.yf, "screen", fake)
    pool = screener.fetch_market("in")
    assert pool["universe"] == ["TCS.NS"] and list(pool["quotes"]) == ["TCS.NS"]
    assert all(not isinstance(q, str) for q, _ in seen)
    assert {kw["sortField"] for _, kw in seen} == {"dayvolume", "percentchange", "intradaymarketcap"}
    assert (("percentchange", True)) in {(kw["sortField"], kw["sortAsc"]) for _, kw in seen}  # losers sort ascending

def test_the_pool_is_cached_per_market(monkeypatch):
    calls = []
    monkeypatch.setattr(screener.yf, "screen", lambda *a, **k: calls.append(1) or {"quotes": [_quote("AAA")]})
    screener.fetch_market("us")
    n = len(calls)
    screener.fetch_market("us")
    assert len(calls) == n  # cached
    screener.fetch_market("in")
    assert len(calls) > n  # another market has its own cache entry (and finds no .NS names here)
    assert screener.fetch_universe("in") == []

def test_daily_cycle_picks_analyst_names_and_labels_them():
    broker = FakeBroker(cash=100_000, prices={"BUYME": 50.0, "MOVE": 20.0})
    seen = []
    rec = run_daily(broker, scores={"MOVE": 9.0}, analyst={"BUYME": 1.5}, n=1, analyse=lambda t: seen.append(t) or DECISION,
                    plan_fn=lambda c: PortfolioPlan(summary="s", intents=[]), log=lambda *_: None)
    assert seen == ["BUYME", "MOVE"] and rec["screen"]["BUYME"]["why"].startswith("analyst recommended buy")
    assert rec["screen"]["MOVE"]["why"] == "top mover / volume spike"
