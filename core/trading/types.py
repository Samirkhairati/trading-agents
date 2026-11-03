"""Shared data types for the paper-trading layer. Plain dataclasses, no logic."""
from dataclasses import dataclass, field
from typing import Literal, Optional

@dataclass
class Account:
    cash: float
    equity: float  # cash + market value of positions

@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    price: float  # current price

    @property
    def market_value(self) -> float:
        return self.qty * self.price

    @property
    def pnl_pct(self) -> float:
        return (self.price / self.avg_entry - 1) * 100 if self.avg_entry else 0.0

@dataclass
class ProposedOrder:
    symbol: str
    side: Literal["buy", "sell"]
    notional: float = 0.0  # dollars to buy (side == "buy")
    entry_price: float = 0.0  # reference price used for sizing / stop validation
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""  # short LLM / rule explanation, kept in the trade log

@dataclass
class Rejection:
    order: ProposedOrder
    reason: str

@dataclass
class RiskConfig:
    max_position_pct: float = 10.0  # max % of equity in one symbol (after the buy)
    min_cash_pct: float = 10.0  # cash that must remain after buys
    max_positions: int = 15
    max_new_per_day: int = 2  # new symbols opened in one cycle; adding to a holding doesn't count
    min_order_usd: float = 50.0
    halt_drawdown_pct: float = 10.0  # block new buys if equity this far below its peak
    require_stop: bool = True
