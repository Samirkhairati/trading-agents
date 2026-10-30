from agents.base import BaseAgent
from agents.schemas import AnalystOutput

class TechnicalAnalyst(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are a trading assistant tasked with analyzing financial markets. Your role is to evaluate the most relevant "
            "technical indicators (Moving Averages, MACD, RSI, Bollinger Bands, Volume) provided in the context below. "
            "Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights "
            "with supporting evidence to help traders make informed decisions. "
            "Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read. "
            "Do not claim historical validation or exact percentage moves unless they are directly supported by the data."
        )
        super().__init__(role_prompt=role_prompt, model=model)
        
    def analyze_technicals(self, price_data_str: str) -> AnalystOutput:
        data_context = (
            f"Here is the recent OHLCV price action and computed technical indicators (SMA, EMA, RSI, MACD):\n\n{price_data_str}\n\n"
            "Analyze this data deeply, give a score from 1-10 (1=bearish, 10=bullish), and provide your comprehensive markdown report in the reasoning field."
        )
        return self.analyze(data_context, AnalystOutput)
