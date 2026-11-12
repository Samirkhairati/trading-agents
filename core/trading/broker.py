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
    def can_short(self, symbol: str) -> bool: ...


class FakeBroker:
    """In-memory broker: orders fill instantly at the price dict. A short is a holding with a negative quantity;
    its sale proceeds go into cash (the risk gate keeps them from being spent)."""

    def __init__(self, cash: float = 100_000, prices: Optional[dict[str, float]] = None):
        self.cash = float(cash)
        self.prices: dict[str, float] = dict(prices or {})
        self.holdings: dict[str, tuple[float, float]] = {}  # symbol -> (qty, avg_entry); qty < 0 = short
        self.exits: dict[str, tuple[Optional[float], Optional[float]]] = {}  # symbol -> (stop, take) for check_exits()
        self.orders: list[ProposedOrder] = []

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_price(self, symbol: str) -> float:
        if symbol not in self.prices:
            raise ValueError(f"No price for {symbol}")
        return self.prices[symbol]

    def can_short(self, symbol: str) -> bool:
        return True

    def get_positions(self) -> list[Position]:
        return [Position(s, q, avg, self.get_price(s)) for s, (q, avg) in self.holdings.items()]

    def get_account(self) -> Account:
        value = sum(p.market_value for p in self.get_positions())
        return Account(cash=self.cash, equity=self.cash + value)

    def place_order(self, order: ProposedOrder) -> str:
        if order.side in ("sell", "cover"):
            return self.close_position(order.symbol)
        price = self.get_price(order.symbol)
        old_qty, old_avg = self.holdings.get(order.symbol, (0.0, 0.0))
        if order.side == "short":
            if old_qty > 0:
                raise ValueError(f"{order.symbol} is held long; sell it before shorting")
            qty = order.notional / price
            new_qty = old_qty - qty
            self.holdings[order.symbol] = (new_qty, (-old_qty * old_avg + qty * price) / -new_qty)
            self.cash += order.notional
        else:
            if old_qty < 0:
                raise ValueError(f"{order.symbol} is held short; cover it before buying")
            if order.notional > self.cash + 1e-9:
                raise ValueError(f"Insufficient cash: need {order.notional:.2f}, have {self.cash:.2f}")
            qty = order.notional / price
            new_qty = old_qty + qty
            self.holdings[order.symbol] = (new_qty, (old_qty * old_avg + qty * price) / new_qty)
            self.cash -= order.notional
        if order.stop_loss is not None or order.take_profit is not None:
            self.exits[order.symbol] = (order.stop_loss, order.take_profit)
        self.orders.append(order)
        return f"fake-{uuid.uuid4().hex[:8]}"

    def close_position(self, symbol: str) -> str:
        if symbol not in self.holdings:
            raise ValueError(f"No position in {symbol}")
        qty, _ = self.holdings.pop(symbol)
        self.exits.pop(symbol, None)
        self.cash += qty * self.get_price(symbol)  # a short (qty < 0) pays the price to buy the shares back
        self.orders.append(ProposedOrder(symbol=symbol, side="cover" if qty < 0 else "sell", reason="close"))
        return f"fake-{uuid.uuid4().hex[:8]}"

    def check_exits(self) -> list[tuple[str, str]]:
        """Close every position whose stop-loss or take-profit has been reached. Returns [(symbol, 'stop' | 'take')]."""
        hit = []
        for sym, (qty, _) in list(self.holdings.items()):
            stop, take = self.exits.get(sym, (None, None))
            try:
                price = self.get_price(sym)
            except ValueError:
                continue
            short = qty < 0
            if stop is not None and (price >= stop if short else price <= stop):
                kind = "stop"
            elif take is not None and (price <= take if short else price >= take):
                kind = "take"
            else:
                continue
            self._close_on_exit(sym, kind, price)
            hit.append((sym, kind))
        return hit

    def _close_on_exit(self, symbol: str, kind: str, price: float) -> None:
        self.close_position(symbol)


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

    def can_short(self, symbol: str) -> bool:
        """Alpaca only lets paper accounts short assets it can borrow; anything we cannot verify counts as not shortable."""
        try:
            asset = self.trading_client.get_asset(symbol)
            return bool(asset.shortable and asset.easy_to_borrow)
        except Exception:
            return False

    def place_order(self, order: ProposedOrder) -> str:
        if order.side in ("sell", "cover"):
            return self.close_position(order.symbol)
        short = order.side == "short"
        side = OrderSide.SELL if short else OrderSide.BUY
        if order.stop_loss is None and order.take_profit is None and not short:
            req = MarketOrderRequest(
                symbol=order.symbol, notional=round(order.notional, 2),
                side=side, time_in_force=TimeInForce.DAY,
            )
        else:
            price = order.entry_price or self.get_price(order.symbol)
            qty = math.floor(order.notional / price)
            if qty < 1:
                raise ValueError(
                    f"{'Short' if short else 'Bracket'} order needs whole shares: ${order.notional:.2f} is <1 share of {order.symbol} at {price:.2f}"
                )
            both = order.stop_loss is not None and order.take_profit is not None
            req = MarketOrderRequest(
                # GTC so the stop/take legs outlive the day the order was placed
                symbol=order.symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
                order_class=OrderClass.BRACKET if both else OrderClass.OTO if (order.stop_loss is not None or order.take_profit is not None) else None,
                take_profit=TakeProfitRequest(limit_price=round(order.take_profit, 2)) if order.take_profit is not None else None,
                stop_loss=StopLossRequest(stop_price=round(order.stop_loss, 2)) if order.stop_loss is not None else None,
            )
        return str(self.trading_client.submit_order(req).id)

    def recent_fills(self, limit: int = 50) -> list[dict]:
        """Filled orders (newest first), including stop-loss / take-profit legs that Alpaca executed on its own."""
        orders = self.trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit, direction="desc"))
        out = []
        for o in orders:
            qty = float(o.filled_qty or 0)
            if qty <= 0 or o.filled_at is None:
                continue
            price = float(o.filled_avg_price or 0)
            kind = str(getattr(o.order_type, "value", o.order_type) or "market")
            out.append({"time": o.filled_at.isoformat(timespec="seconds"), "type": str(getattr(o.side, "value", o.side)).lower(),
                        "symbol": o.symbol, "qty": qty, "price": price, "amount": qty * price, "pnl": None,
                        "note": {"stop": "stop-loss hit", "limit": "take-profit / limit hit"}.get(kind, "")})
        return out

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
