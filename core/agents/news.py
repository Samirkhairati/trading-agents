from agents.base import BaseAgent
from agents.schemas import AnalystOutput

class NewsAnalyst(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are a macro-economic news researcher tasked with analyzing recent trends over the past week. "
            "Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. "
            "You will be given raw data from FRED (Federal Reserve Economic Data) such as inflation and unemployment, "
            "as well as live market-implied probabilities of forward-looking events from Polymarket. "
            "Provide specific, actionable insights with supporting evidence to help traders make informed decisions. "
            "Make sure to append a Markdown table at the end of the report to organize key points in the report."
        )
        super().__init__(role_prompt=role_prompt, model=model)
        
    def analyze_macro(self, fred_str: str, polymarket_str: str) -> AnalystOutput:
        data_context = (
            f"### Macro Indicators (FRED)\n<start_of_fred>\n{fred_str}\n<end_of_fred>\n\n"
            f"### Prediction Markets (Polymarket)\n<start_of_polymarket>\n{polymarket_str}\n<end_of_polymarket>\n\n"
            "Analyze this data deeply, give a score from 1-10 (1=bearish, 10=bullish), and provide your comprehensive markdown report in the reasoning field."
        )
        return self.analyze(data_context, AnalystOutput)
