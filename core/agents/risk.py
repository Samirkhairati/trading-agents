from agents.base import BaseAgent
from agents.schemas import RiskOutput

class RiskAgent(BaseAgent):
    def __init__(self, risk_profile: str, model: str = None):
        if risk_profile not in ["Aggressive", "Conservative", "Neutral"]:
            raise ValueError("Risk profile must be Aggressive, Conservative, or Neutral")
            
        role_prompt = (
            f"You are a {risk_profile} Risk Manager for a quantitative trading firm. "
            "Your job is to evaluate the proposed trade from your specific risk perspective. "
            "Review the analyst reports and the bull/bear debate history. "
            "Assess the downside risks, volatility, and determine a safe maximum position size. "
            "Provide a detailed risk assessment."
        )
        super().__init__(role_prompt=role_prompt, model=model)
        
    def assess_risk(self, context: str) -> RiskOutput:
        return self.analyze(context, RiskOutput)
