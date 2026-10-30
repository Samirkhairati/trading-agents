from agents.base import BaseAgent, deep_model
from agents.schemas import TradingDecision

class PortfolioManager(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are the Head Portfolio Manager for a quantitative trading firm. "
            "Your job is to make the final trading decision (Buy, Overweight, Hold, Underweight or Sell) for a given stock. "
            "You will receive reports from Fundamental, Technical, and Sentiment analysts, "
            "a transcript of a debate between Bull and Bear researchers, "
            "and a final risk assessment from the Risk Management team. "
            "Weigh all this evidence carefully. Also consider any past decision on this stock supplied in the context. Output your rating, target price, stop loss, position size and a justification."
        )
        super().__init__(role_prompt=role_prompt, model=model or deep_model())
        
    def make_decision(self, context: str) -> TradingDecision:
        return self.analyze(context, TradingDecision)
