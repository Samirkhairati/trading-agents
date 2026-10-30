import os

from graph.state import AgentState

MAX_DEBATE_ROUNDS = int(os.environ.get("DEBATE_ROUNDS", "2"))  # rounds per side (each costs 2 LLM calls)
MAX_RISK_ROUNDS = 1  # passes of the three risk views

# Every node a router can return. Passed to add_conditional_edges as an explicit path map.
DEBATE_PATH_MAP = {n: n for n in ("Bull Researcher", "Bear Researcher", "Research Manager")}
RISK_PATH_MAP = {n: n for n in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager")}

def should_continue_debate(state: AgentState) -> str:
    if state.get("debate_count", 0) >= 2 * MAX_DEBATE_ROUNDS:
        return "Research Manager"
    return "Bear Researcher" if state.get("last_debater") == "Bull" else "Bull Researcher"

def should_continue_risk(state: AgentState) -> str:
    if state.get("risk_count", 0) >= 3 * MAX_RISK_ROUNDS:
        return "Portfolio Manager"
    nxt = {"Aggressive": "Conservative Analyst", "Conservative": "Neutral Analyst"}
    return nxt.get(state.get("last_risk_speaker", ""), "Aggressive Analyst")
