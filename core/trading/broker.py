"""Broker layer: a Protocol, an in-memory FakeBroker, and an Alpaca paper-trading AlpacaBroker."""
import math
import os
import uuid
from typing import Optional, Protocol

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest

from trading.types import Account, Position, ProposedOrder


class Broker(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_price(self, symbol: str) -> float: ...
    def place_order(self, order: ProposedOrder) -> str: ...
    def close_position(self, symbol: str) -> str: ...


class FakeBroker:
    """In-memory broker: orders fill instantly at the price dict."""

    def __init__(self, cash: float = 100_000, prices: Optional[dict[str, float]] = None):
        self.cash = float(cash)
        self.prices: dict[str, float] = dict(prices or {})
        self.holdings: dict[str, tuple[float, float]] = {}  # symbol -> (qty, avg_entry)
        self.orders: list[ProposedOrder] = []

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_price(self, symbol: str) -> float:
        if symbol not in self.prices:
            raise ValueError(f"No price for {symbol}")
        return self.prices[symbol]

    def get_positions(self) -> list[Position]:
        return [Position(s, q, avg, self.get_price(s)) for s, (q, avg) in self.holdings.items()]

    def get_account(self) -> Account:
        value = sum(p.market_value for p in self.get_positions())
        return Account(cash=self.cash, equity=self.cash + value)

    def place_order(self, order: ProposedOrder) -> str:
        if order.side == "sell":
            return self.close_position(order.symbol)
        price = self.get_price(order.symbol)
        if order.notional > self.cash + 1e-9:
            raise ValueError(f"Insufficient cash: need {order.notional:.2f}, have {self.cash:.2f}")
        qty = order.notional / price
        old_qty, old_avg = self.holdings.get(order.symbol, (0.0, 0.0))
        new_qty = old_qty + qty
        self.holdings[order.symbol] = (new_qty, (old_qty * old_avg + qty * price) / new_qty)
        self.cash -= order.notional
        self.orders.append(order)
        return f"fake-{uuid.uuid4().hex[:8]}"

    def close_position(self, symbol: str) -> str:
        if symbol not in self.holdings:
            raise ValueError(f"No position in {symbol}")
        qty, _ = self.holdings.pop(symbol)
        self.cash += qty * self.get_price(symbol)
        self.orders.append(ProposedOrder(symbol=symbol, side="sell", reason="close"))
        return f"fake-{uuid.uuid4().hex[:8]}"


class AlpacaBroker:
    """Alpaca paper trading. Construction makes no network call."""

    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None):
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys) in core/.env")
        self.trading_client = TradingClient(api_key, secret_key, paper=True)
        self.data_client = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self) -> Account:
        acct = self.trading_client.get_account()
        return Account(cash=float(acct.cash), equity=float(acct.equity))

    def get_positions(self) -> list[Position]:
        return [
            Position(p.symbol, float(p.qty), float(p.avg_entry_price), float(p.current_price))
            for p in self.trading_client.get_all_positions()
        ]

    def get_price(self, symbol: str) -> float:
        trades = self.data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(trades[symbol].price)

    def is_market_open(self) -> bool:
        return bool(self.trading_client.get_clock().is_open)

    def place_order(self, order: ProposedOrder) -> str:
        if order.side == "sell":
            return self.close_position(order.symbol)
        if order.stop_loss is None and order.take_profit is None:
            req = MarketOrderRequest(
                symbol=order.symbol, notional=round(order.notional, 2),
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            )
        else:
            price = order.entry_price or self.get_price(order.symbol)
            qty = math.floor(order.notional / price)
            if qty < 1:
                raise ValueError(
                    f"Bracket order needs whole shares: ${order.notional:.2f} buys <1 share of {order.symbol} at {price:.2f}"
                )
            both = order.stop_loss is not None and order.take_profit is not None
            req = MarketOrderRequest(
                # GTC so the stop/take legs outlive the day the order was placed
                symbol=order.symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                order_class=OrderClass.BRACKET if both else OrderClass.OTO,
                take_profit=TakeProfitRequest(limit_price=round(order.take_profit, 2)) if order.take_profit is not None else None,
                stop_loss=StopLossRequest(stop_price=round(order.stop_loss, 2)) if order.stop_loss is not None else None,
            )
        return str(self.trading_client.submit_order(req).id)

    def close_position(self, symbol: str) -> str:
        # open stop/take legs reserve the shares, so Alpaca refuses to close until they are cancelled
        for o in self.trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            try:
                self.trading_client.cancel_order_by_id(o.id)
            except Exception:
                pass  # a sibling leg may already be cancelled
        return str(self.trading_client.close_position(symbol).id)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    broker = AlpacaBroker()
    print(broker.get_account())
    for pos in broker.get_positions():
        print(pos)
