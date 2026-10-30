"""Portfolio planner: turns analysis ratings + account state into order intents, then into ProposedOrders."""
from typing import Dict, List

from agents.base import BaseAgent, deep_model
from agents.schemas import PortfolioPlan
from trading.types import Account, Position, ProposedOrder

DEFAULT_STOP_PCT = 7.0
DEFAULT_TAKE_PCT = 12.0

class PortfolioAgent(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are the portfolio manager of a small PAPER-trading long-only equity portfolio. You receive the account, current "
            "positions, and fresh analyst decisions (rating, target, stop) for a few tickers. Decide per ticker: buy (open or add), "
            "sell (close), or hold. Use target_weight_pct as the desired weight after the action; keep positions few and concentrated "
            "only in high-conviction names, keep meaningful cash, never exceed 10% in one stock. Buy/Overweight ratings may justify "
            "buys, Hold means keep, Underweight/Sell mean sell if held and skip if not. A buy must include a stop_loss below the current "
            "price and a take_profit above it. Only use tickers from the provided lists. Every intent needs the exact fields "
            "symbol (not 'ticker'), action, target_weight_pct, stop_loss, take_profit, reason; reason is never optional."
        )
        super().__init__(role_prompt=role_prompt, model=model or deep_model())

    def plan(self, context: str) -> PortfolioPlan:
        return self.analyze(context, PortfolioPlan)

def build_context(account: Account, positions: List[Position], decisions: Dict[str, dict], past_trades: str = "") -> str:
    held = "\n".join(
        f"- {p.symbol}: {p.qty:g} sh, avg {p.avg_entry:.2f}, now {p.price:.2f} ({p.pnl_pct:+.1f}%), "
        f"weight {p.market_value / account.equity * 100:.1f}%" for p in positions
    ) or "(none)"
    ratings = "\n".join(
        f"- {t}: {d['rating']}, price target {d.get('target_price')}, stop {d.get('stop_loss')}, "
        f"suggested size {d.get('position_size')}% | {str(d.get('justification', ''))[:300]}" for t, d in decisions.items()
    ) or "(none)"
    return (
        f"Equity ${account.equity:,.2f}, cash ${account.cash:,.2f} ({account.cash / account.equity * 100:.1f}%)\n\n"
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
