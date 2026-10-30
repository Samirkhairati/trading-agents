import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DEBATE_ROUNDS", "2")  # load_dotenv never overrides, so tests ignore .env

from agents import schemas as s  # noqa: E402

CANNED = {
    s.AnalystOutput: s.AnalystOutput(score=6, reasoning="canned report"),
    s.SentimentReport: s.SentimentReport(overall_band="Neutral", overall_score=5.0, confidence="low", narrative="canned"),
    s.DebateOutput: s.DebateOutput(argument="canned argument"),
    s.ResearchPlan: s.ResearchPlan(recommendation="Buy", rationale="r", strategic_actions="a"),
    s.TraderProposal: s.TraderProposal(action="Buy", reasoning="t", entry_price=100.0, target_price=120.0, stop_loss=90.0),
    s.RiskOutput: s.RiskOutput(risk_assessment="ok", max_position_size=5.0, risk_score=4),
    s.TradingDecision: s.TradingDecision(rating="Overweight", target_price=120.0, stop_loss=90.0, position_size=5.0, justification="canned"),
}

def make_price_df(n: int = 260) -> pd.DataFrame:
    from stockstats import wrap
    from data.fetcher import INDICATORS

    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, n))
    df = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1_000_000}, index=idx)
    df = wrap(df)
    for ind in INDICATORS:
        df[ind]
    return pd.DataFrame(df)

@pytest.fixture
def fake_llm(monkeypatch):
    """BaseAgent.analyze returns a canned model; records which response models were requested."""
    calls = []

    def fake_analyze(self, ctx, response_format):
        calls.append(response_format)
        return CANNED[response_format]

    monkeypatch.setattr("agents.base.BaseAgent.analyze", fake_analyze)
    monkeypatch.setattr("agents.base.instructor.from_litellm", lambda *a, **k: None)
    return calls

@pytest.fixture
def fake_data(monkeypatch):
    import graph.trading_graph as tg

    df = make_price_df()
    monkeypatch.setattr(tg, "fetch_price_history", lambda t, *a, **k: df)
    monkeypatch.setattr(tg, "fetch_fundamentals", lambda t: {"Name": "Fake Inc"})
    monkeypatch.setattr(tg, "fetch_news", lambda t, n=5: [{"title": "headline", "providerPublishTime": "today"}])
    monkeypatch.setattr(tg, "fetch_stocktwits_messages", lambda t: "bullish")
    monkeypatch.setattr(tg, "fetch_fred_macro_data", lambda: "cpi 3%")
    monkeypatch.setattr(tg, "fetch_polymarket_odds", lambda t: "none")
    return df

@pytest.fixture(autouse=True)
def tmp_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISIONS_PATH", str(tmp_path / "decisions.json"))
