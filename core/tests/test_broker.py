"""Broker tests: FakeBroker behaviour and AlpacaBroker request building. No network."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

import trading.broker as broker_mod
from trading.broker import AlpacaBroker, FakeBroker
from trading.types import ProposedOrder


def buy(symbol="AAPL", notional=1000.0, **kw) -> ProposedOrder:
    return ProposedOrder(symbol=symbol, side="buy", notional=notional, **kw)


def test_buy_fills_at_price():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    b.place_order(buy(notional=1000))
    (pos,) = b.get_positions()
    assert pos.symbol == "AAPL" and pos.qty == pytest.approx(10) and pos.avg_entry == pytest.approx(100)
    assert b.get_account().cash == pytest.approx(9_000)


def test_average_entry_updates_on_add():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    b.place_order(buy(notional=1000))  # 10 @ 100
    b.set_price("AAPL", 50.0)
    b.place_order(buy(notional=1000))  # 20 @ 50
    (pos,) = b.get_positions()
    assert pos.qty == pytest.approx(30)
    assert pos.avg_entry == pytest.approx(2000 / 30)


def test_equity_uses_current_prices():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    b.place_order(buy(notional=1000))
    b.set_price("AAPL", 110.0)
    acct = b.get_account()
    assert acct.cash == pytest.approx(9_000)
    assert acct.equity == pytest.approx(9_000 + 10 * 110)


def test_sell_order_closes_whole_position():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    b.place_order(buy(notional=1000))
    b.set_price("AAPL", 120.0)
    b.place_order(ProposedOrder(symbol="AAPL", side="sell"))
    assert b.get_positions() == []
    assert b.get_account().cash == pytest.approx(10_200)


def test_close_position_returns_id_and_clears():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    b.place_order(buy(notional=500))
    assert isinstance(b.close_position("AAPL"), str)
    assert b.get_positions() == []
    with pytest.raises(ValueError):
        b.close_position("AAPL")


def test_insufficient_cash():
    b = FakeBroker(cash=500, prices={"AAPL": 100.0})
    with pytest.raises(ValueError, match="Insufficient cash"):
        b.place_order(buy(notional=1000))
    assert b.get_positions() == [] and b.cash == 500


def test_unknown_price():
    b = FakeBroker(cash=10_000)
    with pytest.raises(ValueError):
        b.place_order(buy("ZZZ"))
    with pytest.raises(ValueError):
        b.get_price("ZZZ")


def test_stop_and_take_recorded():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    b.place_order(buy(stop_loss=95.0, take_profit=110.0, reason="test"))
    assert len(b.orders) == 1
    assert b.orders[0].stop_loss == 95.0 and b.orders[0].take_profit == 110.0


def test_alpaca_requires_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_API_KEY"):
        AlpacaBroker()


@pytest.fixture
def alpaca(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    tc, dc = MagicMock(), MagicMock()
    monkeypatch.setattr(broker_mod, "TradingClient", tc)
    monkeypatch.setattr(broker_mod, "StockHistoricalDataClient", dc)
    return AlpacaBroker(), tc, dc


def test_alpaca_construct_makes_no_calls(alpaca):
    b, tc, dc = alpaca
    tc.assert_called_once_with("fake-key", "fake-secret", paper=True)
    dc.assert_called_once()
    assert b.trading_client.method_calls == [] and b.data_client.method_calls == []


def test_alpaca_bracket_order_request(alpaca):
    b, _, _ = alpaca
    b.trading_client.submit_order.return_value = SimpleNamespace(id="abc-123")
    oid = b.place_order(buy(notional=1000, entry_price=150.0, stop_loss=140.126, take_profit=170.999))
    assert oid == "abc-123"
    req = b.trading_client.submit_order.call_args.args[0]
    assert req.symbol == "AAPL" and req.qty == 6 and req.notional is None  # floor(1000 / 150)
    assert req.side == OrderSide.BUY and req.time_in_force == TimeInForce.GTC  # legs must outlive the day
    assert req.order_class == OrderClass.BRACKET
    assert req.stop_loss.stop_price == 140.13
    assert req.take_profit.limit_price == 171.0


def test_alpaca_bracket_too_small_raises(alpaca):
    b, _, _ = alpaca
    with pytest.raises(ValueError, match="whole shares"):
        b.place_order(buy(notional=100, entry_price=150.0, stop_loss=140.0, take_profit=170.0))
    b.trading_client.submit_order.assert_not_called()


def test_alpaca_plain_buy_uses_notional(alpaca):
    b, _, _ = alpaca
    b.trading_client.submit_order.return_value = SimpleNamespace(id="n-1")
    b.place_order(buy(notional=123.456))
    req = b.trading_client.submit_order.call_args.args[0]
    assert req.notional == 123.46 and req.qty is None and req.order_class is None


def test_alpaca_sell_closes_position(alpaca):
    b, _, _ = alpaca
    b.trading_client.close_position.return_value = SimpleNamespace(id="c-1")
    assert b.place_order(ProposedOrder(symbol="AAPL", side="sell")) == "c-1"
    b.trading_client.close_position.assert_called_once_with("AAPL")


def test_alpaca_close_cancels_open_legs_first(alpaca):
    b, _, _ = alpaca
    tc = b.trading_client
    tc.get_orders.return_value = [SimpleNamespace(id="leg-1"), SimpleNamespace(id="leg-2")]
    tc.cancel_order_by_id.side_effect = [None, RuntimeError("already cancelled")]  # a failed cancel must not abort
    tc.close_position.return_value = SimpleNamespace(id="c-2")
    assert b.close_position("AAPL") == "c-2"
    assert [c.args[0] for c in tc.cancel_order_by_id.call_args_list] == ["leg-1", "leg-2"]
    assert tc.get_orders.call_args.args[0].symbols == ["AAPL"]


def test_alpaca_account_positions_clock(alpaca):
    b, _, _ = alpaca
    b.trading_client.get_account.return_value = SimpleNamespace(cash="900.5", equity="1000")
    b.trading_client.get_all_positions.return_value = [
        SimpleNamespace(symbol="AAPL", qty="2", avg_entry_price="100", current_price="105")
    ]
    b.trading_client.get_clock.return_value = SimpleNamespace(is_open=True)
    b.data_client.get_stock_latest_trade.return_value = {"AAPL": SimpleNamespace(price=101.5)}
    assert b.get_account().equity == 1000 and b.get_account().cash == 900.5
    (pos,) = b.get_positions()
    assert (pos.symbol, pos.qty, pos.avg_entry, pos.price) == ("AAPL", 2.0, 100.0, 105.0)
    assert b.is_market_open() is True
    assert b.get_price("AAPL") == 101.5
