"""Short selling: brokers, risk gate, the 'right situation' guard, planner rules and the daily run. No network, no LLM."""
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from agents.schemas import PlanIntent, PortfolioPlan
from trading import sim, state as st
from trading.broker import AlpacaBroker, FakeBroker
from trading.daily_run import run_daily
from trading.planner import add_rule_intents, intents_to_orders
from trading.risk import check_orders
from trading.shorting import short_blockers
from trading.types import Account, Position, ProposedOrder, RiskConfig

CFG = RiskConfig()

@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sim, "_fetch", lambda s: 100.0)
    sim._cache.clear()

def short(symbol="AAA", notional=400.0, entry=100.0, stop=108.0, take=85.0) -> ProposedOrder:
    return ProposedOrder(symbol, "short", notional, entry, stop, take, "test")

def acct(cash=10_000.0, equity=10_000.0) -> Account:
    return Account(cash=cash, equity=equity)

# ---------------------------------------------------------------- brokers

def test_short_gains_when_the_price_falls():
    b = FakeBroker(cash=10_000, prices={"AAA": 100.0})
    b.place_order(short(notional=1_000))
    pos = b.get_positions()[0]
    assert pos.side == "short" and pos.qty == pytest.approx(-10) and b.cash == pytest.approx(11_000)
    assert b.get_account().equity == pytest.approx(10_000)  # proceeds minus the liability
    b.set_price("AAA", 90.0)
    assert b.get_positions()[0].pnl_pct == pytest.approx(10.0)  # +10% for us
    assert b.get_account().equity == pytest.approx(10_100)
    b.place_order(ProposedOrder("AAA", "cover"))
    assert b.get_positions() == [] and b.cash == pytest.approx(10_100)

def test_short_loses_when_the_price_rises():
    b = FakeBroker(cash=10_000, prices={"AAA": 100.0})
    b.place_order(short(notional=1_000))
    b.set_price("AAA", 120.0)
    assert b.get_positions()[0].pnl_pct == pytest.approx(-20.0)
    assert b.get_account().equity == pytest.approx(9_800)

def test_adding_to_a_short_averages_the_entry():
    b = FakeBroker(cash=10_000, prices={"AAA": 100.0})
    b.place_order(short(notional=1_000))
    b.set_price("AAA", 200.0)
    b.place_order(short(notional=1_000))
    qty, avg = b.holdings["AAA"]
    assert qty == pytest.approx(-15) and avg == pytest.approx(2000 / 15)

def test_cannot_hold_both_sides():
    b = FakeBroker(cash=10_000, prices={"AAA": 100.0, "BBB": 50.0})
    b.place_order(short(notional=500))
    with pytest.raises(ValueError, match="short"):
        b.place_order(ProposedOrder("AAA", "buy", 500, 100))
    b.place_order(ProposedOrder("BBB", "buy", 500, 50))
    with pytest.raises(ValueError, match="long"):
        b.place_order(short("BBB", 500, 50, 54, 42))

def test_check_exits_fires_stops_and_targets_on_both_sides():
    b = FakeBroker(cash=10_000, prices={"L": 100.0, "S": 100.0, "T": 100.0})
    b.place_order(ProposedOrder("L", "buy", 1_000, 100, 93, 112))
    b.place_order(short("S", 1_000, 100, 108, 85))
    b.place_order(short("T", 1_000, 100, 108, 85))
    assert b.check_exits() == []
    b.set_price("L", 92.0)   # long stop
    b.set_price("S", 109.0)  # short stop is ABOVE the entry
    b.set_price("T", 84.0)   # short take-profit is below it
    assert sorted(b.check_exits()) == [("L", "stop"), ("S", "stop"), ("T", "take")]
    assert b.get_positions() == [] and b.exits == {}

def test_sim_account_persists_shorts_and_stops_per_market():
    us, india = sim.SimBroker("us"), sim.SimBroker("in")
    assert us.cash == 100_000 and india.cash == 10_000_000  # separate accounts and currencies
    us.place_order(short("AAA", 1_000, 100, 108, 85))
    again = sim.SimBroker("us")
    assert again.holdings["AAA"][0] == pytest.approx(-10) and again.exits["AAA"] == (108, 85)
    assert sim.SimBroker("in").holdings == {}  # India's state is not touched
    sim._cache.clear()
    sim._fetch = lambda s: 110.0
    assert again.check_exits() == [("AAA", "stop")]
    last = sim.SimBroker("us").ledger[-1]
    assert last["type"] == "cover" and last["source"] == "stop" and last["pnl"] == pytest.approx(-100.0)

def test_alpaca_short_is_a_sell_bracket_with_whole_shares():
    ab = AlpacaBroker("k", "s")
    ab.trading_client = MagicMock()
    ab.trading_client.submit_order.return_value = NS(id="o1")
    ab.data_client = MagicMock()
    ab.place_order(short("AAA", 1_050, 100, 108, 85))
    req = ab.trading_client.submit_order.call_args[0][0]
    assert req.side.value == "sell" and req.qty == 10 and req.order_class.value == "bracket"
    assert req.stop_loss.stop_price == 108 and req.take_profit.limit_price == 85
    with pytest.raises(ValueError, match="whole shares"):
        ab.place_order(short("AAA", 50, 100, 108, 85))
    ab.trading_client.get_asset.return_value = NS(shortable=True, easy_to_borrow=False)
    assert ab.can_short("AAA") is False

# ---------------------------------------------------------------- risk gate

def test_clean_short_is_approved_and_sized_within_caps():
    ok, bad = check_orders([short(notional=400)], acct(), [], CFG)
    assert [o.notional for o in ok] == [400] and bad == []

def test_short_stop_must_be_above_entry_and_target_below():
    _, bad = check_orders([short(stop=95)], acct(), [], CFG)
    assert bad[0].reason.startswith("invalid_stop") and "above entry" in bad[0].reason
    _, bad = check_orders([short(take=110)], acct(), [], CFG)
    assert bad[0].reason.startswith("invalid_take_profit") and "below entry" in bad[0].reason
    _, bad = check_orders([ProposedOrder("AAA", "short", 400, 100, None, 85)], acct(), [], CFG)
    assert bad[0].reason.startswith("require_stop")

def test_short_is_resized_to_the_per_stock_cap():
    ok, _ = check_orders([short(notional=2_000)], acct(), [], CFG)  # cap: 5% of 10,000
    assert ok[0].notional == pytest.approx(500) and "max_short_position_pct" in ok[0].reason

def test_total_short_exposure_is_capped():
    cfg = RiskConfig(max_short_positions=10, max_short_exposure_pct=30.0, max_short_position_pct=20.0)
    held = [Position(s, -5.0, 100.0, 100.0) for s in ("A", "B", "C", "D")]  # $500 each: 20% of equity already short
    ok, _ = check_orders([short("E", 1_500)], Account(cash=12_000, equity=10_000), held, cfg)
    assert ok[0].notional == pytest.approx(1_000) and "max_short_exposure_pct" in ok[0].reason  # 30% of 10,000 minus the 2,000
    full = [Position(s, -6.0, 100.0, 100.0) for s in "ABCDE"]  # $600 each: exactly 30%
    ok, bad = check_orders([short("F", 800)], Account(cash=13_000, equity=10_000), full, cfg)
    assert ok == [] and bad[0].reason.startswith("max_short_exposure_pct")

def test_short_count_and_new_per_day_limits():
    held = [Position(s, -1.0, 100.0, 100.0) for s in ("A", "B", "C", "D", "E")]
    _, bad = check_orders([short("F")], acct(12_000, 10_000), held, CFG)
    assert bad[0].reason.startswith("max_short_positions")
    ok, bad = check_orders([short("X"), short("Y")], acct(), [], CFG)  # one new short per cycle
    assert [o.symbol for o in ok] == ["X"] and bad[0].reason.startswith("max_new_shorts_per_day")

def test_adding_to_an_existing_short_is_not_a_new_short():
    held = [Position("AAA", -1.0, 100.0, 100.0)]
    ok, _ = check_orders([short("AAA", 300), short("NEW", 300)], Account(cash=10_100, equity=10_000), held, CFG)
    assert [o.symbol for o in ok] == ["AAA", "NEW"]

def test_short_proceeds_cannot_fund_longs():
    held = [Position("S", -50.0, 100.0, 100.0)]  # $5,000 short: cash is 10,000 equity + 5,000 proceeds
    cfg = RiskConfig(max_position_pct=100.0)
    ok, bad = check_orders([ProposedOrder("L", "buy", 9_500, 100, 95, 110)], Account(cash=15_000, equity=10_000), held, cfg)
    assert ok[0].notional == pytest.approx(9_000) and "min_cash_pct" in ok[0].reason  # 10,000 - 10% floor, the 5,000 is locked

def test_cover_always_passes_even_when_halted_and_frees_nothing_spendable():
    held = [Position("AAA", -10.0, 100.0, 100.0)]
    ok, bad = check_orders([ProposedOrder("AAA", "cover")], Account(cash=11_000, equity=10_000), held, CFG, peak_equity=20_000)
    assert len(ok) == 1 and bad == []

def test_halt_blocks_new_shorts():
    _, bad = check_orders([short()], acct(8_000, 8_000), [], CFG, peak_equity=10_000)
    assert bad[0].reason.startswith("drawdown_halt") and "shorts" in bad[0].reason

def test_cannot_short_what_is_held_long_or_buy_what_is_short():
    _, bad = check_orders([short("AAA")], acct(), [Position("AAA", 1.0, 100.0, 100.0)], CFG)
    assert bad[0].reason.startswith("opposite_position")
    _, bad = check_orders([ProposedOrder("AAA", "buy", 300, 100, 95, 110)], acct(), [Position("AAA", -1.0, 100.0, 100.0)], CFG)
    assert bad[0].reason.startswith("opposite_position")

def test_shorts_can_be_switched_off():
    _, bad = check_orders([short()], acct(), [], RiskConfig(allow_shorts=False))
    assert bad[0].reason.startswith("shorts_disabled")

# ---------------------------------------------------------------- the right situation

GOOD = {"price": 90.0, "sma50": 100.0, "ret_1m": -8.0, "analyst": 3.1, "short_float_pct": 4.0, "days_to_cover": 2.0, "earnings_days": 30.0}

def test_short_when_sell_rated_in_a_downtrend_and_nothing_risky():
    assert short_blockers("Sell", GOOD) == []

@pytest.mark.parametrize("rating,facts,shortable,needle", [
    ("Underweight", {}, None, "rating is Underweight"),
    ("Hold", {}, None, "rating is Hold"),
    ("Sell", {"price": 105.0}, None, "above its 50-day average"),
    ("Sell", {"ret_1m": 15.0}, None, "rallying"),
    ("Sell", {"analyst": 1.6}, None, "favourite"),
    ("Sell", {"short_float_pct": 25.0}, None, "squeeze"),
    ("Sell", {"days_to_cover": 9.0}, None, "days to cover"),
    ("Sell", {"earnings_days": 2.0}, None, "earnings in 2 days"),
    ("Sell", {}, False, "cannot borrow"),
    ("Sell", {"price": None}, None, "no price history"),
])
def test_every_short_blocker(rating, facts, shortable, needle):
    why = short_blockers(rating, {**GOOD, **facts}, shortable)
    assert any(needle in w for w in why), why

def test_unknown_facts_do_not_block():
    unknown = {"price": 90.0, "sma50": None, "ret_1m": None, "analyst": None, "short_float_pct": None, "days_to_cover": None, "earnings_days": None}
    assert short_blockers("Sell", unknown) == []

# ---------------------------------------------------------------- planner

def intent(symbol, action, weight=3.0, **kw) -> PlanIntent:
    return PlanIntent(symbol=symbol, action=action, target_weight_pct=weight, reason="r", **kw)

def test_short_intent_becomes_an_order_with_short_side_stops():
    plan = PortfolioPlan(summary="s", intents=[intent("AAA", "short", 4, stop_loss=90.0, take_profit=120.0)])  # long-style stops from a small model
    [o] = intents_to_orders(plan, Account(cash=100_000, equity=100_000), [], {"AAA": 100.0})
    assert o.side == "short" and o.notional == 4_000 and o.stop_loss == 108.0 and o.take_profit == 85.0
    plan = PortfolioPlan(summary="s", intents=[intent("AAA", "short", 4, stop_loss=104.0, take_profit=80.0)])
    [o] = intents_to_orders(plan, Account(cash=100_000, equity=100_000), [], {"AAA": 100.0})
    assert o.stop_loss == 104.0 and o.take_profit == 80.0  # sensible ones are kept

def test_cover_buy_and_sell_all_close_a_short_and_short_on_a_long_is_dropped():
    held = [Position("S", -10.0, 100.0, 100.0), Position("T", -10.0, 100.0, 100.0), Position("U", -10.0, 100.0, 100.0), Position("L", 10.0, 100.0, 100.0)]
    plan = PortfolioPlan(summary="s", intents=[intent("S", "cover", 0), intent("T", "buy", 5), intent("U", "sell", 0), intent("L", "short", 3)])
    orders = intents_to_orders(plan, Account(cash=100_000, equity=100_000), held, {s: 100.0 for s in "STUL"})
    assert [(o.symbol, o.side) for o in orders] == [("S", "cover"), ("T", "cover"), ("U", "cover")]

def test_orders_are_closes_then_buys_then_shorts():
    plan = PortfolioPlan(summary="s", intents=[intent("N", "short"), intent("B", "buy", 5), intent("OLD", "sell", 0)])
    held = [Position("OLD", 10.0, 50.0, 60.0)]
    orders = intents_to_orders(plan, Account(cash=100_000, equity=100_000), held, {"N": 100.0, "B": 100.0})
    assert [o.side for o in orders] == ["sell", "buy", "short"]

def test_rules_short_sell_rated_names_and_cover_contradicted_shorts():
    held = [Position("SHORTED", -10.0, 100.0, 100.0), Position("LONG", 10.0, 100.0, 100.0)]
    decisions = {"SELLME": {"rating": "Sell", "position_size": 2.0}, "SHORTED": {"rating": "Buy"}, "LONG": {"rating": "Sell"},
                 "MEH": {"rating": "Hold"}, "BIGSIZE": {"rating": "Sell", "position_size": 40.0}, "NOSIZE": {"rating": "Sell", "position_size": 0}}
    plan = PortfolioPlan(summary="s", intents=[intent("MEH", "hold", 0), intent("SELLME", "hold", 0)])
    out = add_rule_intents(plan, decisions, held, CFG)
    acts = {i.symbol: (i.action, i.target_weight_pct) for i in out.intents}
    assert acts["SELLME"] == ("short", 2.0)  # a 'hold' on a stock we do not own does not stop the rule
    assert acts["SHORTED"] == ("cover", 0)
    assert acts["BIGSIZE"] == ("short", 5.0) and acts["NOSIZE"] == ("short", 3.0)  # capped / defaulted
    assert "LONG" not in acts and acts["MEH"] == ("hold", 0)  # selling a held long is the LLM's call
    assert len(plan.intents) == 2  # the original plan is untouched

def test_rules_respect_the_llm_when_it_already_shorted_or_shorts_are_off():
    plan = PortfolioPlan(summary="s", intents=[intent("X", "short", 4)])
    out = add_rule_intents(plan, {"X": {"rating": "Sell"}}, [], CFG)
    assert [(i.action, i.target_weight_pct) for i in out.intents] == [("short", 4)]
    assert add_rule_intents(plan, {"Y": {"rating": "Sell"}}, [], RiskConfig(allow_shorts=False)) is plan

# ---------------------------------------------------------------- the daily run

SELL = {"rating": "Sell", "target_price": 80.0, "stop_loss": 110.0, "position_size": 3.0, "justification": "weak"}
BUY = {"rating": "Buy", "target_price": 120.0, "stop_loss": 90.0, "position_size": 5.0, "justification": "strong"}
def no_plan(ctx): return PortfolioPlan(summary="nothing", stance="do_nothing", intents=[])

def test_sell_rated_stock_in_a_downtrend_is_shorted_and_logged():
    b = FakeBroker(cash=100_000, prices={"AAA": 90.0})
    rec = run_daily(b, execute=True, tickers=["AAA"], analyse=lambda t: SELL, plan_fn=no_plan, short_facts=lambda s: GOOD, log=lambda *_: None)
    assert [(o["side"], o["symbol"]) for o in rec["approved"]] == [("short", "AAA")] and rec["rejected"] == []
    pos = b.get_positions()[0]
    assert pos.side == "short" and abs(pos.market_value) == pytest.approx(3_000, rel=0.01) and b.exits["AAA"][0] > 90.0

def test_a_rallying_stock_rated_sell_is_not_shorted():
    b = FakeBroker(cash=100_000, prices={"AAA": 90.0})
    facts = {**GOOD, "price": 120.0, "sma50": 100.0, "ret_1m": 25.0}
    rec = run_daily(b, execute=True, tickers=["AAA"], analyse=lambda t: SELL, plan_fn=no_plan, short_facts=lambda s: facts, log=lambda *_: None)
    assert rec["approved"] == [] and b.get_positions() == []
    assert rec["rejected"][0]["reason"].startswith("short_guard") and "above its 50-day average" in rec["rejected"][0]["reason"]

def test_unshortable_stock_is_not_shorted():
    b = FakeBroker(cash=100_000, prices={"AAA": 90.0})
    b.can_short = lambda s: False
    rec = run_daily(b, execute=True, tickers=["AAA"], analyse=lambda t: SELL, plan_fn=no_plan, short_facts=lambda s: GOOD, log=lambda *_: None)
    assert rec["approved"] == [] and "cannot borrow" in rec["rejected"][0]["reason"]

def test_dry_run_shorts_nothing_and_shorting_can_be_switched_off(monkeypatch):
    b = FakeBroker(cash=100_000, prices={"AAA": 90.0})
    rec = run_daily(b, tickers=["AAA"], analyse=lambda t: SELL, plan_fn=no_plan, short_facts=lambda s: GOOD, log=lambda *_: None)
    assert len(rec["approved"]) == 1 and b.get_positions() == []
    monkeypatch.setenv("ALLOW_SHORTS", "0")
    rec = run_daily(b, execute=True, tickers=["AAA"], analyse=lambda t: SELL, plan_fn=no_plan, short_facts=lambda s: GOOD, log=lambda *_: None)
    assert rec["approved"] == [] and b.get_positions() == []

def test_a_short_is_covered_when_the_new_rating_is_buy():
    b = FakeBroker(cash=100_000, prices={"AAA": 90.0})
    b.holdings["AAA"] = (-10.0, 100.0)
    b.cash += 1_000
    rec = run_daily(b, execute=True, tickers=["AAA"], analyse=lambda t: BUY, plan_fn=no_plan, short_facts=lambda s: GOOD, log=lambda *_: None)
    assert [(o["side"], o["symbol"]) for o in rec["approved"]] == [("cover", "AAA")] and b.get_positions() == []

def test_stops_fire_at_the_start_of_an_executing_cycle():
    b = FakeBroker(cash=100_000, prices={"AAA": 110.0})
    b.holdings["AAA"] = (-10.0, 100.0)
    b.exits["AAA"] = (108.0, 85.0)
    run_daily(b, execute=True, tickers=[], analyse=lambda t: BUY, plan_fn=no_plan, short_facts=lambda s: GOOD, log=lambda *_: None)
    assert b.get_positions() == []
