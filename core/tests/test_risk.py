"""Risk gate tests: every rule, cumulative batch state, no network."""
import pytest

from trading.risk import check_orders, drawdown_pct, update_peak
from trading.types import Account, Position, ProposedOrder, RiskConfig

CFG = RiskConfig()  # 10% position, 10% cash, 5 positions, $50 min, 10% halt


def buy(symbol="AAPL", notional=500.0, entry=100.0, stop=95.0, take=110.0) -> ProposedOrder:
    return ProposedOrder(symbol, "buy", notional, entry, stop, take, "test")


def sell(symbol="AAPL") -> ProposedOrder:
    return ProposedOrder(symbol, "sell", reason="exit")


def acct(cash=10_000.0, equity=10_000.0) -> Account:
    return Account(cash=cash, equity=equity)


def pos(symbol, value) -> Position:
    return Position(symbol, qty=value / 100, avg_entry=100.0, price=100.0)


def test_empty_orders():
    assert check_orders([], acct(), [], CFG) == ([], [])


def test_clean_buy_approved_unchanged():
    o = buy(notional=500)
    ok, bad = check_orders([o], acct(), [], CFG)
    assert ok == [o] and ok[0] is o and bad == []


def test_helpers():
    assert drawdown_pct(90, 100) == pytest.approx(10)
    assert drawdown_pct(110, 100) == 0.0
    assert drawdown_pct(50, 0) == 0.0
    assert update_peak(100, None) == 100
    assert update_peak(120, 100) == 120
    assert update_peak(80, 100) == 100


def test_drawdown_halt_blocks_buys():
    ok, bad = check_orders([buy()], acct(equity=9_000, cash=9_000), [], CFG, peak_equity=10_000)
    assert ok == [] and len(bad) == 1 and "drawdown_halt" in bad[0].reason


def test_no_halt_just_above_threshold_or_without_peak():
    ok, _ = check_orders([buy()], acct(equity=9_100, cash=9_100), [], CFG, peak_equity=10_000)
    assert len(ok) == 1
    ok, _ = check_orders([buy()], acct(equity=1_000, cash=1_000), [], CFG, peak_equity=None)
    assert len(ok) == 1


def test_sells_pass_during_halt():
    held = [pos("AAPL", 500)]
    ok, bad = check_orders([sell("AAPL"), buy("MSFT")], acct(equity=8_000, cash=7_500), held, CFG, peak_equity=10_000)
    assert [o.symbol for o in ok] == ["AAPL"] and ok[0].side == "sell"
    assert len(bad) == 1 and bad[0].order.symbol == "MSFT"


def test_sell_of_unheld_symbol_passes():
    ok, bad = check_orders([sell("NVDA")], acct(), [], CFG)
    assert len(ok) == 1 and bad == []


def test_min_order():
    ok, bad = check_orders([buy(notional=49.99)], acct(), [], CFG)
    assert ok == [] and "min_order" in bad[0].reason


def test_require_stop():
    ok, bad = check_orders([buy(stop=None)], acct(), [], CFG)
    assert ok == [] and "require_stop" in bad[0].reason
    ok, bad = check_orders([buy(stop=None)], acct(), [], RiskConfig(require_stop=False))
    assert len(ok) == 1 and bad == []


@pytest.mark.parametrize("stop", [100.0, 105.0])
def test_stop_must_be_below_entry(stop):
    ok, bad = check_orders([buy(stop=stop)], acct(), [], CFG)
    assert ok == [] and "invalid_stop" in bad[0].reason


@pytest.mark.parametrize("take", [100.0, 90.0])
def test_take_profit_must_be_above_entry(take):
    ok, bad = check_orders([buy(take=take)], acct(), [], CFG)
    assert ok == [] and "invalid_take_profit" in bad[0].reason


def test_take_profit_optional():
    ok, _ = check_orders([buy(take=None)], acct(), [], CFG)
    assert len(ok) == 1


def test_max_positions_new_symbol_rejected():
    held = [pos(s, 100) for s in "ABCDE"]
    ok, bad = check_orders([buy("NEW")], acct(), held, CFG)
    assert ok == [] and "max_positions" in bad[0].reason


def test_max_positions_adding_to_existing_allowed():
    held = [pos(s, 100) for s in "ABCDE"]
    ok, bad = check_orders([buy("A", notional=200)], acct(), held, CFG)
    assert len(ok) == 1 and bad == []


def test_max_positions_counts_earlier_sell_in_batch():
    held = [pos(s, 100) for s in "ABCDE"]
    ok, bad = check_orders([sell("A"), buy("NEW")], acct(), held, CFG)
    assert [o.symbol for o in ok] == ["A", "NEW"] and bad == []


def test_max_positions_counts_earlier_buy_in_batch():
    held = [pos(s, 100) for s in "ABCD"]
    ok, bad = check_orders([buy("N1"), buy("N2")], acct(), held, CFG)
    assert [o.symbol for o in ok] == ["N1"] and "max_positions" in bad[0].reason


def test_position_cap_resizes_without_mutating_original():
    o = buy(notional=1_500)
    ok, bad = check_orders([o], acct(cash=10_000, equity=10_000), [], CFG)
    assert bad == [] and ok[0] is not o
    assert ok[0].notional == pytest.approx(1_000)
    assert o.notional == 1_500 and o.reason == "test"
    assert "max_position_pct" in ok[0].reason and ok[0].reason.startswith("test")
    assert ok[0].stop_loss == o.stop_loss and ok[0].symbol == o.symbol


def test_position_cap_counts_existing_exposure():
    held = [pos("AAPL", 800)]
    ok, bad = check_orders([buy("AAPL", notional=500)], acct(), held, CFG)
    assert ok[0].notional == pytest.approx(200)


def test_position_cap_rejects_when_resized_below_min():
    held = [pos("AAPL", 980)]  # only $20 of room
    ok, bad = check_orders([buy("AAPL", notional=500)], acct(), held, CFG)
    assert ok == [] and "max_position_pct" in bad[0].reason


def test_position_cap_rejects_when_already_over_cap():
    held = [pos("AAPL", 1_500)]
    ok, bad = check_orders([buy("AAPL", notional=500)], acct(), held, CFG)
    assert ok == [] and "max_position_pct" in bad[0].reason


def test_cash_floor_shrinks_to_fit():
    # equity 10k, floor $1k, cash $1,300 -> $300 spendable
    ok, bad = check_orders([buy(notional=800)], acct(cash=1_300, equity=10_000), [], CFG)
    assert bad == [] and ok[0].notional == pytest.approx(300)
    assert "min_cash_pct" in ok[0].reason


def test_cash_floor_rejects_when_spendable_below_min():
    ok, bad = check_orders([buy(notional=500)], acct(cash=1_030, equity=10_000), [], CFG)
    assert ok == [] and "min_cash_pct" in bad[0].reason


def test_cash_floor_applies_cumulatively_across_batch():
    # $1,500 spendable above the floor; two $1,000 buys (each within the 10% cap)
    a = acct(cash=2_500, equity=10_000)
    ok, bad = check_orders([buy("AAA", 1_000), buy("BBB", 1_000)], a, [], CFG)
    assert ok[0].notional == pytest.approx(1_000)
    assert ok[1].notional == pytest.approx(500)
    assert sum(o.notional for o in ok) <= 1_500 + 1e-6 and bad == []
    # a third buy has nothing left
    ok, bad = check_orders([buy("AAA", 1_000), buy("BBB", 1_000), buy("CCC", 500)], a, [], CFG)
    assert len(ok) == 2 and "min_cash_pct" in bad[0].reason


def test_sell_frees_cash_for_later_buy():
    held = [pos("OLD", 1_000)]
    a = acct(cash=1_020, equity=10_000)  # nothing spendable before the sell
    ok, bad = check_orders([buy("NEW", 500)], a, held, CFG)
    assert ok == [] and len(bad) == 1
    ok, bad = check_orders([sell("OLD"), buy("NEW", 500)], a, held, CFG)
    assert [o.side for o in ok] == ["sell", "buy"] and ok[1].notional == pytest.approx(500)


def test_sell_after_buy_in_batch_frees_that_buy_too():
    ok, bad = check_orders([buy("AAA", 800), sell("AAA"), buy("BBB", 800)], acct(cash=1_500, equity=10_000), [], CFG)
    assert [o.symbol for o in ok] == ["AAA", "AAA", "BBB"] and bad == []


def test_cumulative_position_cap_same_symbol_twice():
    ok, bad = check_orders([buy(notional=700), buy(notional=700)], acct(), [], CFG)
    assert ok[0].notional == pytest.approx(700) and ok[1].notional == pytest.approx(300)


def test_both_resizes_chain():
    # cap room 1,000 then cash spendable 400
    ok, _ = check_orders([buy(notional=2_000)], acct(cash=1_400, equity=10_000), [], CFG)
    assert ok[0].notional == pytest.approx(400)
    assert "max_position_pct" in ok[0].reason and "min_cash_pct" in ok[0].reason


def test_rejection_reasons_are_nonempty_strings():
    held = [pos(s, 100) for s in "ABCDE"]
    orders = [buy(notional=1), buy(stop=None), buy(stop=200), buy(take=1), buy("NEW")]
    ok, bad = check_orders(orders, acct(), held, CFG)
    assert ok == [] and len(bad) == len(orders)
    assert all(isinstance(r.reason, str) and r.reason.strip() for r in bad)
    ok, bad = check_orders([buy()], acct(equity=5_000, cash=5_000), [], CFG, peak_equity=10_000)
    assert bad[0].reason.strip()


def test_inputs_not_mutated():
    held = [pos("AAPL", 500)]
    orders = [sell("AAPL"), buy("MSFT", 5_000)]
    check_orders(orders, acct(), held, CFG)
    assert orders[1].notional == 5_000 and held[0].market_value == 500
