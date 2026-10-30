"""Risk gate: pure functions that veto or resize proposed orders. No I/O."""
from dataclasses import replace
from typing import Optional

from trading.types import Account, Position, ProposedOrder, Rejection, RiskConfig

EPS = 1e-9


def drawdown_pct(equity: float, peak: float) -> float:
    """Percent equity is below its peak (0 when at/above peak or peak unknown)."""
    if peak <= 0 or equity >= peak:
        return 0.0
    return (1 - equity / peak) * 100


def update_peak(equity: float, peak: Optional[float]) -> float:
    """New peak equity: the higher of the stored peak and current equity."""
    return equity if peak is None else max(peak, equity)


def check_orders(
    orders: list[ProposedOrder],
    account: Account,
    positions: list[Position],
    cfg: RiskConfig,
    peak_equity: Optional[float] = None,
) -> tuple[list[ProposedOrder], list[Rejection]]:
    """Apply the risk rules in order, cumulatively. Returns (approved, rejected)."""
    equity = account.equity
    cash = account.cash
    exposure = {p.symbol: p.market_value for p in positions if p.qty > 0}
    approved: list[ProposedOrder] = []
    rejected: list[Rejection] = []

    halted = peak_equity is not None and equity <= peak_equity * (1 - cfg.halt_drawdown_pct / 100)

    def reject(order: ProposedOrder, reason: str) -> None:
        rejected.append(Rejection(order, reason))

    for o in orders:
        if o.side == "sell":
            cash += exposure.pop(o.symbol, 0.0)
            approved.append(o)
            continue

        if halted:
            dd = drawdown_pct(equity, peak_equity)
            reject(o, f"drawdown_halt: equity ${equity:,.2f} is {dd:.1f}% below peak "
                      f"${peak_equity:,.2f} (halt at {cfg.halt_drawdown_pct:g}%); new buys blocked")
            continue
        if o.notional < cfg.min_order_usd:
            reject(o, f"min_order: notional ${o.notional:,.2f} < minimum ${cfg.min_order_usd:,.2f}")
            continue
        if cfg.require_stop and o.stop_loss is None:
            reject(o, "require_stop: buy has no stop_loss")
            continue
        if o.stop_loss is not None and o.stop_loss >= o.entry_price:
            reject(o, f"invalid_stop: stop_loss {o.stop_loss:g} must be below entry {o.entry_price:g} for a long")
            continue
        if o.take_profit is not None and o.take_profit <= o.entry_price:
            reject(o, f"invalid_take_profit: take_profit {o.take_profit:g} must be above entry {o.entry_price:g}")
            continue
        if o.symbol not in exposure and len(exposure) >= cfg.max_positions:
            reject(o, f"max_positions: {len(exposure)} positions held, limit is {cfg.max_positions}")
            continue

        notional = o.notional
        notes: list[str] = []

        room = equity * cfg.max_position_pct / 100 - exposure.get(o.symbol, 0.0)
        if notional > room + EPS:
            if room < cfg.min_order_usd:
                reject(o, f"max_position_pct: only ${max(room, 0):,.2f} of room under the "
                          f"{cfg.max_position_pct:g}% cap, below minimum order ${cfg.min_order_usd:,.2f}")
                continue
            notes.append(f"max_position_pct: resized ${notional:,.2f} -> ${room:,.2f} "
                         f"({cfg.max_position_pct:g}% cap)")
            notional = room

        spendable = cash - equity * cfg.min_cash_pct / 100
        if notional > spendable + EPS:
            if spendable < cfg.min_order_usd:
                reject(o, f"min_cash_pct: only ${max(spendable, 0):,.2f} spendable above the "
                          f"{cfg.min_cash_pct:g}% cash floor, below minimum order ${cfg.min_order_usd:,.2f}")
                continue
            notes.append(f"min_cash_pct: resized ${notional:,.2f} -> ${spendable:,.2f} "
                         f"({cfg.min_cash_pct:g}% cash floor)")
            notional = spendable

        if notes:
            o = replace(o, notional=notional, reason=(o.reason + " " if o.reason else "") + "[" + "; ".join(notes) + "]")
        cash -= notional
        exposure[o.symbol] = exposure.get(o.symbol, 0.0) + notional
        approved.append(o)

    return approved, rejected
