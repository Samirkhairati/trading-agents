import memory
from agents.schemas import PortfolioRating, TradingDecision
from data.fetcher import format_price_report
from graph.conditional_logic import should_continue_debate, should_continue_risk, DEBATE_PATH_MAP, RISK_PATH_MAP
from graph.trading_graph import TOTAL_STAGES, run_analysis
from tests.conftest import make_price_df

def test_price_report_has_indicators():
    text = format_price_report(make_price_df())
    assert "RSI" in text and "close_50_sma" in text

def test_rating_enum_rejects_junk():
    import pytest
    with pytest.raises(Exception):
        TradingDecision(rating="Moon", justification="x")
    assert PortfolioRating("Buy")

def test_routing_terminates_with_valid_nodes():
    state, seen = {}, []
    node = should_continue_debate(state)
    while node != "Research Manager":
        assert node in DEBATE_PATH_MAP
        state = {"debate_count": state.get("debate_count", 0) + 1, "last_debater": node.split()[0]}
        node = should_continue_debate(state)
        seen.append(node)
    assert len(seen) == 4
    state, node, n = {}, should_continue_risk({}), 0
    while node != "Portfolio Manager":
        assert node in RISK_PATH_MAP
        n += 1
        state = {"risk_count": n, "last_risk_speaker": node.split()[0]}
        node = should_continue_risk(state)
    assert n == 3

def test_full_graph_and_memory(fake_llm, fake_data):
    progress = []
    state = run_analysis("fake", on_progress=lambda s, d, t: progress.append((s, d, t)))
    assert state["final_decision"]["rating"] == "Overweight"
    assert len(state["debate_history"]) == 4 and len(state["risk_history"]) == 3
    assert progress[-1][0] == "Portfolio Manager" and progress[-1][1] == TOTAL_STAGES
    assert len(memory.get_history("FAKE")) == 1
    second = run_analysis("fake")
    assert "Overweight" in second["past_context"]

def test_api_job_lifecycle(fake_llm, fake_data):
    import time
    from fastapi.testclient import TestClient
    import main

    c = TestClient(main.app)
    job_id = c.post("/api/analyze", json={"ticker": "fake"}).json()["job_id"]
    for _ in range(50):
        st = c.get(f"/api/status/{job_id}").json()
        if st["state"] != "running":
            break
        time.sleep(0.1)
    assert st["state"] == "done", st
    assert c.get(f"/api/result/{job_id}").json()["final_decision"]["rating"] == "Overweight"
    assert c.get("/api/status/nope").status_code == 404
    assert c.get("/api/history/fake").json()["history"]
