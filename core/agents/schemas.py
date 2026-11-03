from __future__ import annotations
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------
class PortfolioRating(str, Enum):
    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"

class TraderAction(str, Enum):
    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"

# ---------------------------------------------------------------------------
# Research Manager (Debater/Planner)
# ---------------------------------------------------------------------------
class ResearchPlan(BaseModel):
    recommendation: PortfolioRating = Field(description="The investment recommendation. Exactly one of Buy / Overweight / Hold / Underweight / Sell.")
    rationale: str = Field(description="Conversational summary of the key points from both sides of the debate, ending with which arguments led to the recommendation.")
    strategic_actions: str = Field(description="Concrete steps for the trader to implement the recommendation, including position sizing guidance.")

# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------
class SentimentBand(str, Enum):
    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"

class SentimentReport(BaseModel):
    overall_band: SentimentBand = Field(description="Overall sentiment direction. Exactly one of: Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish.")
    overall_score: float = Field(ge=0.0, le=10.0, description="Numeric sentiment intensity on a 0-10 scale. 0 = maximally bearish, 5 = neutral, 10 = maximally bullish.")
    confidence: Literal["low", "medium", "high"] = Field(description="Confidence in the assessment based on data quality and sample size.")
    narrative: str = Field(description="Full sentiment report covering source-by-source breakdown, cross-source divergences, dominant themes, catalysts and risks, and a markdown summary table.")

def render_sentiment_report(report: SentimentReport) -> str:
    return f"**Overall Sentiment:** **{report.overall_band.value}** (Score: {report.overall_score:.1f}/10)\n**Confidence:** {report.confidence.capitalize()}\n\n{report.narrative}"

# ---------------------------------------------------------------------------
# General Analyst Output
# ---------------------------------------------------------------------------
class AnalystOutput(BaseModel):
    score: int = Field(ge=1, le=10, description="Score from 1 to 10. 1 is extremely bearish, 5 is neutral, 10 is extremely bullish.")
    reasoning: str = Field(description="Detailed markdown report containing the analyst's findings.")

# ---------------------------------------------------------------------------
# Debate, Trader, Risk, Portfolio Manager
# ---------------------------------------------------------------------------
class DebateOutput(BaseModel):
    argument: str = Field(description="Your argument for this round, rebutting the opponent's last points with evidence from the reports.")

class TraderProposal(BaseModel):
    action: TraderAction = Field(description="Exactly one of Buy / Hold / Sell.")
    reasoning: str = Field(description="Why this action, grounded in the investment plan and analyst reports.")
    entry_price: Optional[float] = Field(default=None, description="Suggested entry price, if applicable.")
    target_price: Optional[float] = Field(default=None, description="Price target, if applicable.")
    stop_loss: Optional[float] = Field(default=None, description="Stop-loss price, if applicable.")

class RiskOutput(BaseModel):
    risk_assessment: str = Field(description="Your risk view of the trader's proposal from your risk profile's perspective.")
    max_position_size: float = Field(ge=0.0, le=100.0, description="Maximum position size as a percent of the portfolio (0-100).")
    risk_score: int = Field(ge=1, le=10, description="1 = very low risk, 10 = extremely high risk.")

class TradingDecision(BaseModel):
    rating: PortfolioRating = Field(description="Final rating. Exactly one of Buy / Overweight / Hold / Underweight / Sell.")
    target_price: Optional[float] = Field(default=None, description="Price target in the stock's currency.")
    stop_loss: Optional[float] = Field(default=None, description="Stop-loss price.")
    position_size: Optional[float] = Field(default=None, description="Recommended position size as percent of portfolio.")
    justification: str = Field(description="Final rationale, referencing the debate, risk views and any past decisions.")

# ---------------------------------------------------------------------------
# Portfolio planner (paper trading)
# ---------------------------------------------------------------------------
class PlanIntent(BaseModel):
    symbol: str = Field(description="Ticker symbol, e.g. AAPL.")
    action: Literal["buy", "sell", "hold"] = Field(description="buy = open/add, sell = close the position, hold = no change.")
    target_weight_pct: float = Field(ge=0.0, le=100.0, description="Desired weight of this stock in the portfolio after the action, as % of equity (0 for sell).")
    stop_loss: Optional[float] = Field(default=None, description="Stop-loss price (below the current price for a buy).")
    take_profit: Optional[float] = Field(default=None, description="Take-profit price (above the current price for a buy).")
    reason: str = Field(description="One or two sentences explaining the decision.")

class PortfolioPlan(BaseModel):
    intents: list[PlanIntent] = Field(description="One entry per stock you want to act on or explicitly hold. Never invent tickers outside the provided lists.")
    stance: Optional[Literal["enter_new", "maintain", "rebalance", "do_nothing"]] = Field(default=None, description="Overall decision: enter_new = open at least one new position, maintain = keep what we hold (may add or trim), rebalance = sell some and buy others, do_nothing = no orders at all.")
    summary: str = Field(description="Short overview of the portfolio stance and cash level.")
