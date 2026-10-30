"""Smoke test: one real Gemini call through the Technical Analyst. Usage: python test.py [TICKER]"""
import sys
from dotenv import load_dotenv

load_dotenv()

from data.fetcher import fetch_price_history, format_price_report
from agents.technical import TechnicalAnalyst

ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
report = format_price_report(fetch_price_history(ticker))
print(report, "\n")
out = TechnicalAnalyst().analyze_technicals(report)
print(f"Score: {out.score}/10\n\n{out.reasoning}")
