import json
import logging
import os
from datetime import date
from typing import Any, Callable, Dict, Optional

from langgraph.graph import END, START, StateGraph

import memory
from agents.debate import DebateAgent
from agents.fundamental import FundamentalAnalyst
from agents.manager import PortfolioManager
from agents.news import NewsAnalyst
from agents.research_manager import ResearchManager
from agents.risk import RiskAgent
from agents.sentiment import SentimentAnalyst
from agents.technical import TechnicalAnalyst
from agents.trader import Trader
from data.alternative_fetchers import (
    fetch_fred_macro_data,
    fetch_polymarket_odds,
    fetch_reddit_posts,
    fetch_stocktwits_messages,
)
from data.fetcher import fetch_fundamentals, fetch_news, fetch_price_history, format_price_report
from graph.conditional_logic import (
    DEBATE_PATH_MAP,
    MAX_DEBATE_ROUNDS,
    MAX_RISK_ROUNDS,
    RISK_PATH_MAP,
    should_continue_debate,
    should_continue_risk,
)
from graph.state import AgentState

logger = logging.getLogger(__name__)

ANALYSTS = ["Fundamental Analyst", "Technical Analyst", "Sentiment Analyst", "News Analyst"]
TOTAL_STAGES = len(ANALYSTS) + 2 * MAX_DEBATE_ROUNDS + 1 + 1 + 3 * MAX_RISK_ROUNDS + 1

def _soft(fn: Callable, *args) -> str:
    """Run a data fetch; a failure becomes text for the analyst instead of aborting the run."""
    try:
        out = fn(*args)
        return out if isinstance(out, str) else json.dumps(out, default=str)[:6000]
    except Exception as e:
        logger.warning("%s failed: %s", getattr(fn, "__name__", fn), e)
        return f"<data unavailable: {e}>"

def _clip(text: str, n: int = 1500) -> str:
    """Keep downstream prompts small (Groq's free tier allows 8K tokens/minute)."""
    return text if len(text) <= n else text[:n] + " ..."

def _reports(state: AgentState) -> str:
    return (
        f"Ticker: {state['ticker']} (as of {state['trade_date']})\n\n"
        f"## Fundamentals\n{_clip(state.get('fundamentals_report', ''))}\n\n"
        f"## Technicals\n{_clip(state.get('market_report', ''))}\n\n"
        f"## Sentiment\n{_clip(state.get('sentiment_report', ''))}\n\n"
        f"## Macro / News\n{_clip(state.get('news_report', ''))}"
    )

# --- Analyst nodes -----------------------------------------------------------
def fundamental_node(state: AgentState) -> dict:
    data = _soft(fetch_fundamentals, state["ticker"])
    out = FundamentalAnalyst().analyze_fundamentals(data)
    return {"fundamentals_report": f"Score {out.score}/10\n{out.reasoning}"}

def technical_node(state: AgentState) -> dict:
    price_text = format_price_report(fetch_price_history(state["ticker"]))  # no data -> fail the run
    out = TechnicalAnalyst().analyze_technicals(price_text)
    return {"market_report": f"Score {out.score}/10\n{out.reasoning}", "price_report": price_text}

def sentiment_node(state: AgentState) -> dict:
    t = state["ticker"]
    news = _soft(lambda: "\n".join(f"- {n['title']} ({n['providerPublishTime']})" for n in fetch_news(t, 8)) or "No news found.")
    stocktwits = _soft(fetch_stocktwits_messages, t)
    reddit = _soft(fetch_reddit_posts, t) if os.environ.get("ENABLE_REDDIT") else "Reddit disabled."
    out = SentimentAnalyst().analyze_sentiment(news, stocktwits, reddit)
    return {"sentiment_report": f"{out.overall_band.value} ({out.overall_score:.1f}/10, {out.confidence} confidence)\n{out.narrative}"}

def news_node(state: AgentState) -> dict:
    out = NewsAnalyst().analyze_macro(_soft(fetch_fred_macro_data), _soft(fetch_polymarket_odds, "interest rates"))
    return {"news_report": f"Score {out.score}/10\n{out.reasoning}"}

# --- Debate nodes ------------------------------------------------------------
def _debate_node(side: str):
    agent = None

    def node(state: AgentState) -> dict:
        nonlocal agent
        agent = agent or DebateAgent(side=side)
        history = state.get("debate_history", [])
        context = _reports(state) + "\n\n## Debate so far\n" + ("\n".join(history) or "(you speak first)")
        arg = agent.debate(context).argument
        return {
            "debate_history": history + [f"{side}: {_clip(arg, 1200)}"],
            "debate_count": state.get("debate_count", 0) + 1,
            "last_debater": side,
        }
    return node

def research_manager_node(state: AgentState) -> dict:
    context = _reports(state) + "\n\n## Debate\n" + "\n".join(state.get("debate_history", []))
    plan = ResearchManager().decide(context)
    return {"investment_plan": f"Recommendation: {plan.recommendation.value}\n{_clip(plan.rationale, 1500)}\nActions: {_clip(str(plan.strategic_actions), 500)}"}

def trader_node(state: AgentState) -> dict:
    context = f"{_reports(state)}\n\n## Investment plan\n{state['investment_plan']}\n\n## Price data\n{chr(10).join(state.get('price_report', '').splitlines()[:2])}"
    p = Trader().propose(context)
    return {"trader_plan": (
        f"Action: {p.action.value}; entry {p.entry_price}, target {p.target_price}, stop {p.stop_loss}\n{_clip(p.reasoning, 1000)}"
    )}

def _risk_node(profile: str):
    agent = None

    def node(state: AgentState) -> dict:
        nonlocal agent
        agent = agent or RiskAgent(risk_profile=profile)
        history = state.get("risk_history", [])
        context = (
            f"{_reports(state)}\n\n## Trader proposal\n{state['trader_plan']}\n\n"
            "## Risk discussion so far\n" + ("\n".join(history) or "(you speak first)")
        )
        r = agent.assess_risk(context)
        entry = f"{profile}: {_clip(r.risk_assessment, 1000)} (max position {r.max_position_size:.0f}%, risk {r.risk_score}/10)"
        return {
            "risk_history": history + [entry],
            "risk_count": state.get("risk_count", 0) + 1,
            "last_risk_speaker": profile,
        }
    return node

def portfolio_manager_node(state: AgentState) -> dict:
    context = (
        f"{_reports(state)}\n\n## Investment plan\n{state['investment_plan']}\n\n## Trader proposal\n{state['trader_plan']}\n\n"
        "## Risk team\n" + "\n".join(state.get("risk_history", [])) +
        f"\n\n## Your past decisions\n{state.get('past_context', 'None')}"
    )
    return {"final_decision": PortfolioManager().make_decision(context).model_dump(mode="json")}

# --- Graph -------------------------------------------------------------------
def build_graph():
    g = StateGraph(AgentState)
    for name, fn in zip(ANALYSTS, [fundamental_node, technical_node, sentiment_node, news_node]):
        g.add_node(name, fn)
        g.add_edge(START, name)  # analysts run in parallel
    g.add_node("Bull Researcher", _debate_node("Bull"))
    g.add_node("Bear Researcher", _debate_node("Bear"))
    g.add_node("Research Manager", research_manager_node)
    g.add_node("Trader", trader_node)
    g.add_node("Aggressive Analyst", _risk_node("Aggressive"))
    g.add_node("Conservative Analyst", _risk_node("Conservative"))
    g.add_node("Neutral Analyst", _risk_node("Neutral"))
    g.add_node("Portfolio Manager", portfolio_manager_node)

    g.add_edge(ANALYSTS, "Bull Researcher")  # join: wait for all analysts
    for n in ("Bull Researcher", "Bear Researcher"):
        g.add_conditional_edges(n, should_continue_debate, DEBATE_PATH_MAP)
    g.add_edge("Research Manager", "Trader")
    g.add_edge("Trader", "Aggressive Analyst")
    for n in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
        g.add_conditional_edges(n, should_continue_risk, RISK_PATH_MAP)
    g.add_edge("Portfolio Manager", END)
    return g.compile()

def run_analysis(
    ticker: str,
    trade_date: Optional[str] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Run the full pipeline and return the final state (JSON-serialisable). `trade_date` is a label only."""
    ticker = ticker.strip().upper()
    state: Dict[str, Any] = {"ticker": ticker, "trade_date": trade_date or date.today().isoformat()}

    # Fail fast on a bad ticker before spending any LLM calls.
    price_df = fetch_price_history(ticker)
    last_price = float(price_df["close"].iloc[-1])
    state["past_context"] = memory.get_past_context(ticker, last_price)

    done = 0
    for update in build_graph().stream(state, stream_mode="updates"):
        for node, delta in update.items():
            state.update(delta or {})
            done += 1
            if on_progress:
                on_progress(node, done, TOTAL_STAGES)

    memory.save_decision(ticker, state["final_decision"], last_price)
    state["last_price"] = last_price
    return state
