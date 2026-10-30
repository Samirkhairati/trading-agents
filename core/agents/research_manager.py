from agents.base import BaseAgent, deep_model
from agents.schemas import ResearchPlan

class ResearchManager(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are the Research Manager and debate judge. Evaluate the Bull/Bear debate and the analyst reports critically. "
            "Commit to a clear recommendation (Buy / Overweight / Hold / Underweight / Sell); do not default to Hold "
            "unless the evidence is genuinely balanced. Give a rationale and concrete strategic actions for the trader."
        )
        super().__init__(role_prompt=role_prompt, model=model or deep_model())

    def decide(self, context: str) -> ResearchPlan:
        return self.analyze(context, ResearchPlan)
