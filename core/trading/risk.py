"""Risk gate: pure functions that veto or resize proposed orders. No I/O.
Sells and covers (reducing risk) always pass; buys and shorts go through every rule."""
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
    longs = {p.symbol: p.market_value for p in positions if p.qty > 0}
    shorts = {p.symbol: -p.market_value for p in positions if p.qty < 0}  # dollars short, positive
    locked = sum(shorts.values())  # short proceeds sit in cash as collateral and cannot fund new positions
    approved: list[ProposedOrder] = []
    rejected: list[Rejection] = []
    opened = opened_short = 0

    halted = peak_equity is not None and equity <= peak_equity * (1 - cfg.halt_drawdown_pct / 100)

    def reject(order: ProposedOrder, reason: str) -> None:
        rejected.append(Rejection(order, reason))

    for o in orders:
        if o.side == "sell":
            cash += longs.pop(o.symbol, 0.0)
            approved.append(o)
            continue
        if o.side == "cover":
            owed = shorts.pop(o.symbol, 0.0)
            cash -= owed
            locked -= owed
            approved.append(o)
            continue

        short = o.side == "short"
        if halted:
            dd = drawdown_pct(equity, peak_equity)
            reject(o, f"drawdown_halt: equity ${equity:,.2f} is {dd:.1f}% below peak "
                      f"${peak_equity:,.2f} (halt at {cfg.halt_drawdown_pct:g}%); new {'shorts' if short else 'buys'} blocked")
            continue
        if short and not cfg.allow_shorts:
            reject(o, "shorts_disabled: short selling is switched off")
            continue
        if o.notional < cfg.min_order_usd:
            reject(o, f"min_order: notional ${o.notional:,.2f} < minimum ${cfg.min_order_usd:,.2f}")
            continue
        if cfg.require_stop and o.stop_loss is None:
            reject(o, f"require_stop: {'short' if short else 'buy'} has no stop_loss")
            continue
        if short:
            if o.stop_loss is not None and o.stop_loss <= o.entry_price:
                reject(o, f"invalid_stop: stop_loss {o.stop_loss:g} must be above entry {o.entry_price:g} for a short")
                continue
            if o.take_profit is not None and o.take_profit >= o.entry_price:
                reject(o, f"invalid_take_profit: take_profit {o.take_profit:g} must be below entry {o.entry_price:g} for a short")
                continue
        else:
            if o.stop_loss is not None and o.stop_loss >= o.entry_price:
                reject(o, f"invalid_stop: stop_loss {o.stop_loss:g} must be below entry {o.entry_price:g} for a long")
                continue
            if o.take_profit is not None and o.take_profit <= o.entry_price:
                reject(o, f"invalid_take_profit: take_profit {o.take_profit:g} must be above entry {o.entry_price:g}")
                continue
        if (o.symbol in shorts and not short) or (o.symbol in longs and short):
            reject(o, f"opposite_position: {o.symbol} is held {'long' if short else 'short'}; close it first")
            continue
        is_new = o.symbol not in longs and o.symbol not in shorts
        if is_new and len(longs) + len(shorts) >= cfg.max_positions:
            reject(o, f"max_positions: {len(longs) + len(shorts)} positions held, limit is {cfg.max_positions}")
            continue
        if short:
            if is_new and len(shorts) >= cfg.max_short_positions:
                reject(o, f"max_short_positions: {len(shorts)} shorts held, limit is {cfg.max_short_positions}")
                continue
            if is_new and opened_short >= cfg.max_new_shorts_per_day:
                reject(o, f"max_new_shorts_per_day: already opening {opened_short} new shorts this cycle, limit is {cfg.max_new_shorts_per_day}")
                continue
        elif is_new and opened >= cfg.max_new_per_day:
            reject(o, f"max_new_per_day: already opening {opened} new positions this cycle, limit is {cfg.max_new_per_day}")
            continue

        notional = o.notional
        notes: list[str] = []

        def cap(rule: str, room: float, what: str) -> bool:
            """Shrink `notional` to `room`; False (and a rejection) if what is left is below the minimum order."""
            nonlocal notional
            if notional <= room + EPS:
                return True
            if room < cfg.min_order_usd:
                reject(o, f"{rule}: only ${max(room, 0):,.2f} of room under the {what}, below minimum order ${cfg.min_order_usd:,.2f}")
                return False
            notes.append(f"{rule}: resized ${notional:,.2f} -> ${room:,.2f} ({what})")
            notional = room
            return True

        if short:
            if not cap("max_short_position_pct", equity * cfg.max_short_position_pct / 100 - shorts.get(o.symbol, 0.0),
                       f"{cfg.max_short_position_pct:g}% short cap per stock"):
                continue
            if not cap("max_short_exposure_pct", equity * cfg.max_short_exposure_pct / 100 - sum(shorts.values()),
                       f"{cfg.max_short_exposure_pct:g}% total short cap"):
                continue
        elif not cap("max_position_pct", equity * cfg.max_position_pct / 100 - longs.get(o.symbol, 0.0), f"{cfg.max_position_pct:g}% cap"):
            continue
        if not cap("min_cash_pct", cash - locked - equity * cfg.min_cash_pct / 100, f"{cfg.min_cash_pct:g}% cash floor"):
            continue

        if notes:
            o = replace(o, notional=notional, reason=(o.reason + " " if o.reason else "") + "[" + "; ".join(notes) + "]")
        if short:
            cash += notional
            locked += notional
            opened_short += is_new
            shorts[o.symbol] = shorts.get(o.symbol, 0.0) + notional
        else:
            cash -= notional
            opened += is_new
            longs[o.symbol] = longs.get(o.symbol, 0.0) + notional
        approved.append(o)

    return approved, rejected
