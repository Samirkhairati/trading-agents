from agents.base import BaseAgent
from agents.schemas import TraderProposal

class Trader(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are a trader turning an investment plan into a concrete trade proposal. "
            "Use the Research Manager's plan and the analyst reports. Choose Buy, Hold or Sell, "
            "and where sensible give entry, target and stop-loss prices based on the price data provided."
        )
        super().__init__(role_prompt=role_prompt, model=model)

    def propose(self, context: str) -> TraderProposal:
        return self.analyze(context, TraderProposal)
