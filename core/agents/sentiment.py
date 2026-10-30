from agents.base import BaseAgent
from agents.schemas import SentimentReport

class SentimentAnalyst(BaseAgent):
    def __init__(self, model: str = None):
        role_prompt = (
            "You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report, "
            "drawing on complementary data sources that have already been collected for you. \n\n"
            "How to analyze this data:\n"
            "1. Read the StockTwits ratio as a leading retail-sentiment signal.\n"
            "2. Look for cross-source divergences. If news is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal.\n"
            "3. Identify recurring narrative themes.\n"
            "4. Be honest about data limits. Flag this explicitly in the confidence field and the narrative.\n"
            "5. Identify catalysts and risks that emerge across sources."
        )
        super().__init__(role_prompt=role_prompt, model=model)
        
    def analyze_sentiment(self, news_str: str, stocktwits_str: str, reddit_str: str) -> SentimentReport:
        data_context = (
            "### News headlines — Yahoo Finance, past 7 days\n"
            f"<start_of_news>\n{news_str}\n<end_of_news>\n\n"
            "### StockTwits messages — retail-trader social platform\n"
            f"<start_of_stocktwits>\n{stocktwits_str}\n<end_of_stocktwits>\n\n"
            "### Reddit posts — r/wallstreetbets, r/stocks, r/investing\n"
            f"<start_of_reddit>\n{reddit_str}\n<end_of_reddit>\n\n"
            "Produce the structured SentimentReport based on this exact data."
        )
        return self.analyze(data_context, SentimentReport)
