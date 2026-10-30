from agents.base import BaseAgent
from agents.schemas import AnalystOutput

class FundamentalAnalyst(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. "
            "Please write a comprehensive report of the company's fundamental information such as financial documents, "
            "company profile, basic company financials, and company financial history to gain a full view of the company's "
            "fundamental information to inform traders. Make sure to include as much detail as possible. "
            "Provide specific, actionable insights with supporting evidence to help traders make informed decisions. "
            "Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
        )
        super().__init__(role_prompt=role_prompt, model=model)
        
    def analyze_fundamentals(self, fundamentals_data_str: str) -> AnalystOutput:
        data_context = (
            f"Here is the pre-fetched fundamental data (Income Statement, Balance Sheet, Cash Flow) for the company:\n\n{fundamentals_data_str}\n\n"
            "Analyze this data deeply, give a score from 1-10 (1=bearish, 10=bullish), and provide your comprehensive markdown report in the reasoning field."
        )
        return self.analyze(data_context, AnalystOutput)
