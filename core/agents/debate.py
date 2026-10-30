from agents.base import BaseAgent
from agents.schemas import DebateOutput

class DebateAgent(BaseAgent):
    def __init__(self, side: str, model: str = None):
        if side not in ["Bull", "Bear"]:
            raise ValueError("Side must be Bull or Bear")
            
        role_prompt = (
            f"You are the {side}ish Researcher for a quantitative trading firm. "
            f"Your job is to debate the opposing researcher regarding the investment potential of a stock. "
            f"You MUST argue from a strictly {side}ish perspective, highlighting why the stock will go {'up' if side == 'Bull' else 'down'}. "
            "You will be given the initial analyst reports (Fundamental, Technical, Sentiment) and the history of the debate so far. "
            "Formulate a strong argument or counter-argument."
        )
        super().__init__(role_prompt=role_prompt, model=model)
        
    def debate(self, context: str) -> DebateOutput:
        return self.analyze(context, DebateOutput)
