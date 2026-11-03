"""Portfolio planner: turns analysis ratings + account state into order intents, then into ProposedOrders."""
from typing import Dict, List

from agents.base import BaseAgent, deep_model
from agents.schemas import PortfolioPlan
from trading.types import Account, Position, ProposedOrder, RiskConfig

DEFAULT_STOP_PCT = 7.0
DEFAULT_TAKE_PCT = 12.0

class PortfolioAgent(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are the portfolio manager of a small simulated long-only equity portfolio. Each day you receive the account, the "
            "current positions, and fresh analyst decisions (rating, target, stop) for two kinds of tickers: stocks we already HOLD and "
            "NEW candidates picked by a screener. First choose one overall stance: enter_new (open at least one new position), "
            "maintain (keep what we hold; may add or trim), rebalance (sell some, buy others) or do_nothing (no orders at all). "
            "Then give one intent per ticker. For a held stock decide: continue (hold), add (buy up to a higher weight) or exit (sell), "
            "using the new rating and how the position has performed. For a new candidate buy only with high conviction and room in "
            "the portfolio; skip it otherwise. Doing nothing is a perfectly good answer when nothing is compelling. Limits: at most "
            f"{RiskConfig.max_positions} positions, at least {RiskConfig.min_cash_pct:g}% cash, at most {RiskConfig.max_position_pct:g}% in one stock. Buy/Overweight ratings may justify buys, Hold means keep, "
            "Underweight/Sell mean sell if held and skip if not. Use target_weight_pct as the weight after the action. A buy must "
            "include a stop_loss below the current price and a take_profit above it. Only use tickers from the provided lists. Every "
            "intent needs the exact fields symbol (not 'ticker'), action, target_weight_pct, stop_loss, take_profit, reason; reason "
            "is never optional."
        )
        super().__init__(role_prompt=role_prompt, model=model or deep_model())

    def plan(self, context: str) -> PortfolioPlan:
        return self.analyze(context, PortfolioPlan)

def build_context(account: Account, positions: List[Position], decisions: Dict[str, dict], past_trades: str = "") -> str:
    held = "\n".join(
        f"- {p.symbol}: {p.qty:g} sh, avg {p.avg_entry:.2f}, now {p.price:.2f} ({p.pnl_pct:+.1f}%), "
        f"weight {p.market_value / account.equity * 100:.1f}%" for p in positions
    ) or "(none)"
    held_syms = {p.symbol for p in positions}
    ratings = "\n".join(
        f"- {t} ({'HELD' if t in held_syms else 'new candidate'}): {d['rating']}, price target {d.get('target_price')}, stop {d.get('stop_loss')}, "
        f"suggested size {d.get('position_size')}% | {str(d.get('justification', ''))[:300]}" for t, d in decisions.items()
    ) or "(none)"
    return (
        f"Equity ${account.equity:,.2f}, cash ${account.cash:,.2f} ({account.cash / account.equity * 100:.1f}%), {len(positions)}/{RiskConfig.max_positions} positions\n\n"
        f"## Current positions\n{held}\n\n## Fresh analysis\n{ratings}\n\n## Recent trades\n{past_trades or '(none)'}"
    )

def intents_to_orders(plan: PortfolioPlan, account: Account, positions: List[Position], prices: Dict[str, float]) -> List[ProposedOrder]:
    """Deterministic conversion. Sells first (they free cash), then buys. Unknown tickers or missing prices are skipped."""
    held = {p.symbol: p for p in positions}
    sells, buys = [], []
    for i in plan.intents:
        sym = i.symbol.upper()
        price = prices.get(sym)
        if i.action == "sell" and sym in held:
            sells.append(ProposedOrder(sym, "sell", entry_price=held[sym].price, reason=i.reason))
        elif i.action == "buy" and price:
            current = held[sym].market_value if sym in held else 0.0
            notional = i.target_weight_pct / 100 * account.equity - current
            if notional <= 0:
                continue
            buys.append(ProposedOrder(
                sym, "buy", notional=round(notional, 2), entry_price=price,
                stop_loss=i.stop_loss or round(price * (1 - DEFAULT_STOP_PCT / 100), 2),
                take_profit=i.take_profit or round(price * (1 + DEFAULT_TAKE_PCT / 100), 2),
                reason=i.reason,
            ))
    return sells + buys
