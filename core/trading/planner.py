"""Portfolio planner: turns analysis ratings + account state into order intents, then into ProposedOrders."""
from typing import Dict, List

from agents.base import BaseAgent, deep_model
from agents.schemas import PlanIntent, PortfolioPlan
from trading.types import Account, Position, ProposedOrder, RiskConfig

DEFAULT_STOP_PCT = 7.0
DEFAULT_TAKE_PCT = 12.0
DEFAULT_SHORT_STOP_PCT = 8.0  # shorts lose without limit, so the default stop is tight
DEFAULT_SHORT_TAKE_PCT = 15.0
DEFAULT_SHORT_WEIGHT_PCT = 3.0

class PortfolioAgent(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are the portfolio manager of a small simulated equity portfolio that can hold long and short positions. Each day you receive the account, the "
            "current positions, and fresh analyst decisions (rating, target, stop) for two kinds of tickers: stocks we already HOLD (long or short) and "
            "NEW candidates picked by a screener. First choose one overall stance: enter_new (open at least one new position), "
            "maintain (keep what we hold; may add or trim), rebalance (sell some, buy others) or do_nothing (no orders at all). "
            "Then give one intent per ticker. For a held long decide: hold, buy (add up to a higher weight) or sell (exit). "
            "For a held short decide: hold, short (add) or cover (close it). Use the new rating and how the position has performed. "
            "For a new candidate: buy only with high conviction and room in the portfolio; "
            "short ONLY a stock rated Sell whose price is already falling, never one that is rallying or that analysts love; skip it otherwise. "
            "Doing nothing is a perfectly good answer when nothing is compelling. Limits: at most "
            f"{RiskConfig.max_positions} positions, at least {RiskConfig.min_cash_pct:g}% cash, at most {RiskConfig.max_position_pct:g}% in one long and "
            f"{RiskConfig.max_short_position_pct:g}% short in one stock ({RiskConfig.max_short_exposure_pct:g}% short in total). "
            "Buy/Overweight ratings may justify buys (or covering a short), Hold means keep, Underweight means sell if held long and skip if not, "
            "Sell means sell if held long and is the only rating that may justify a short. Use target_weight_pct as the weight after the action. "
            "A buy must include a stop_loss below the current price and a take_profit above it; a short must include a stop_loss ABOVE the current price and a "
            "take_profit BELOW it. Only use tickers from the provided lists. Every "
            "intent needs the exact fields symbol (not 'ticker'), action, target_weight_pct, stop_loss, take_profit, reason; reason "
            "is never optional."
        )
        super().__init__(role_prompt=role_prompt, model=model or deep_model())

    def plan(self, context: str) -> PortfolioPlan:
        return self.analyze(context, PortfolioPlan)

def build_context(account: Account, positions: List[Position], decisions: Dict[str, dict], past_trades: str = "", cur: str = "$") -> str:
    def line(p: Position) -> str:
        weight = abs(p.market_value) / account.equity * 100
        return f"- {p.symbol} [{p.side.upper()}]: {abs(p.qty):g} sh, avg {p.avg_entry:.2f}, now {p.price:.2f} ({p.pnl_pct:+.1f}% for us), weight {weight:.1f}%"
    held = "\n".join(line(p) for p in positions) or "(none)"
    kind = {p.symbol: p.side.upper() for p in positions}
    ratings = "\n".join(
        f"- {t} ({'HELD ' + kind[t] if t in kind else 'new candidate'}): {d['rating']}, price target {d.get('target_price')}, stop {d.get('stop_loss')}, "
        f"suggested size {d.get('position_size')}% | {str(d.get('justification', ''))[:300]}" for t, d in decisions.items()
    ) or "(none)"
    shorts = sum(-p.market_value for p in positions if p.qty < 0)
    return (
        f"Equity {cur}{account.equity:,.2f}, cash {cur}{account.cash:,.2f} ({account.cash / account.equity * 100:.1f}%, of which {cur}{shorts:,.2f} is short-sale collateral), "
        f"{len(positions)}/{RiskConfig.max_positions} positions\n\n"
        f"## Current positions\n{held}\n\n## Fresh analysis\n{ratings}\n\n## Recent trades\n{past_trades or '(none)'}"
    )

def add_rule_intents(plan: PortfolioPlan, decisions: Dict[str, dict], positions: List[Position], cfg: RiskConfig) -> PortfolioPlan:
    """Rules the LLM must not forget (a small model often skips them): a held short whose fresh rating is Buy / Overweight is covered,
    and a Sell-rated stock we do not hold gets a short proposed. Anything the LLM already decided with a buy / short / cover is left alone;
    the short guard and the risk gate still veto what is unsafe. Returns a copy; the plan is not mutated."""
    if not cfg.allow_shorts:
        return plan
    held = {p.symbol: p for p in positions}
    acted = {i.symbol.upper(): i.action for i in plan.intents}
    extra: List[PlanIntent] = []
    for sym, d in decisions.items():
        rating = d.get("rating")
        if sym in held and held[sym].qty < 0:
            if rating in ("Buy", "Overweight") and acted.get(sym) != "cover":
                extra.append(PlanIntent(symbol=sym, action="cover", target_weight_pct=0, reason=f"rule: fresh {rating} rating contradicts the short"))
        elif sym not in held and rating == "Sell" and acted.get(sym) not in ("buy", "short"):
            size = d.get("position_size")
            weight = min(size if size and size >= 1 else DEFAULT_SHORT_WEIGHT_PCT, cfg.max_short_position_pct)
            extra.append(PlanIntent(symbol=sym, action="short", target_weight_pct=weight,
                                    reason="rule: Sell rating on a stock we do not hold; the short guard decides if the situation is right"))
    replaced = {e.symbol for e in extra}
    return plan.model_copy(update={"intents": [i for i in plan.intents if i.symbol.upper() not in replaced] + extra})

def intents_to_orders(plan: PortfolioPlan, account: Account, positions: List[Position], prices: Dict[str, float]) -> List[ProposedOrder]:
    """Deterministic conversion. Closes first (they free cash), then buys, then shorts. Unknown tickers or missing prices are skipped.
    A buy or sell on a stock we are short covers it; a short on a stock we hold long is dropped."""
    held = {p.symbol: p for p in positions}
    closes, buys, shorts = [], [], []
    for i in plan.intents:
        sym = i.symbol.upper()
        price = prices.get(sym)
        pos = held.get(sym)
        if i.action in ("sell", "cover") and pos:
            closes.append(ProposedOrder(sym, "cover" if pos.qty < 0 else "sell", entry_price=pos.price, reason=i.reason))
        elif i.action == "buy" and pos and pos.qty < 0:
            closes.append(ProposedOrder(sym, "cover", entry_price=pos.price, reason=i.reason))
        elif i.action == "buy" and price:
            current = pos.market_value if pos else 0.0
            notional = i.target_weight_pct / 100 * account.equity - current
            if notional <= 0:
                continue
            buys.append(ProposedOrder(
                sym, "buy", notional=round(notional, 2), entry_price=price,
                stop_loss=i.stop_loss or round(price * (1 - DEFAULT_STOP_PCT / 100), 2),
                take_profit=i.take_profit or round(price * (1 + DEFAULT_TAKE_PCT / 100), 2),
                reason=i.reason,
            ))
        elif i.action == "short" and price and not (pos and pos.qty > 0):
            notional = i.target_weight_pct / 100 * account.equity - (-pos.market_value if pos else 0.0)
            if notional <= 0:
                continue
            # a stop written long-style (below the price) cannot protect a short, so such a stop or take-profit gets the default
            stop = i.stop_loss if i.stop_loss and i.stop_loss > price else round(price * (1 + DEFAULT_SHORT_STOP_PCT / 100), 2)
            take = i.take_profit if i.take_profit and i.take_profit < price else round(price * (1 - DEFAULT_SHORT_TAKE_PCT / 100), 2)
            shorts.append(ProposedOrder(sym, "short", notional=round(notional, 2), entry_price=price, stop_loss=stop, take_profit=take, reason=i.reason))
    return closes + buys + shorts
