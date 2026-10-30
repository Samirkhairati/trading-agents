from typing import List, TypedDict

class AgentState(TypedDict, total=False):
    ticker: str
    trade_date: str

    # Analyst reports (each analyst writes only its own key, so they can run in parallel)
    fundamentals_report: str
    market_report: str
    sentiment_report: str
    news_report: str
    price_report: str  # raw price/indicator text, reused by the trader

    # Bull/Bear debate
    debate_history: List[str]
    debate_count: int
    last_debater: str

    # Decisions
    investment_plan: str
    trader_plan: str

    # Risk debate
    risk_history: List[str]
    risk_count: int
    last_risk_speaker: str

    past_context: str
    final_decision: dict
