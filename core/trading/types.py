"""Shared data types for the paper-trading layer. Plain dataclasses, no logic."""
from dataclasses import dataclass, field
from typing import Literal, Optional

@dataclass
class Account:
    cash: float  # includes the proceeds of open shorts (they are collateral, the risk gate does not let them be spent)
    equity: float  # cash + market value of positions (shorts count negative)

@dataclass
class Position:
    symbol: str
    qty: float  # negative = short
    avg_entry: float
    price: float  # current price

    @property
    def side(self) -> str:
        return "short" if self.qty < 0 else "long"

    @property
    def market_value(self) -> float:
        return self.qty * self.price  # negative for a short

    @property
    def pnl_pct(self) -> float:
        """Gain in %, from the holder's point of view: a short gains when the price falls."""
        if not self.avg_entry:
            return 0.0
        move = (self.price / self.avg_entry - 1) * 100
        return -move if self.qty < 0 else move

@dataclass
class ProposedOrder:
    symbol: str
    side: Literal["buy", "sell", "short", "cover"]  # buy/sell open/close a long, short/cover open/close a short
    notional: float = 0.0  # amount to buy or short, in the account currency (side buy / short)
    entry_price: float = 0.0  # reference price used for sizing / stop validation
    stop_loss: Optional[float] = None  # below entry for a buy, above entry for a short
    take_profit: Optional[float] = None  # above entry for a buy, below entry for a short
    reason: str = ""  # short LLM / rule explanation, kept in the trade log

@dataclass
class Rejection:
    order: ProposedOrder
    reason: str

@dataclass
class RiskConfig:
    max_position_pct: float = 10.0  # max % of equity in one symbol (after the buy)
    min_cash_pct: float = 10.0  # cash that must remain after buys
    max_positions: int = 15  # longs and shorts together
    max_new_per_day: int = 2  # new symbols opened in one cycle; adding to a holding doesn't count
    min_order_usd: float = 50.0  # smallest order, in the account currency
    halt_drawdown_pct: float = 10.0  # block new buys and shorts if equity this far below its peak
    require_stop: bool = True
    # shorting: unlimited downside, so smaller and fewer than longs
    allow_shorts: bool = True
    max_short_position_pct: float = 5.0  # max % of equity short in one symbol
    max_short_exposure_pct: float = 30.0  # max % of equity short in total
    max_short_positions: int = 5
    max_new_shorts_per_day: int = 1
