"""BaseAgent plumbing: retry on rate limits, token accounting. The provider client is faked; no network."""
from types import SimpleNamespace

import pytest

import agents.base as base
from agents.schemas import AnalystOutput

OK = AnalystOutput(score=5, reasoning="ok")


def raw(tokens=100):
    return SimpleNamespace(usage=SimpleNamespace(total_tokens=tokens))


@pytest.fixture
def agent(monkeypatch):
    slept = []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    monkeypatch.delenv("LLM_TPM", raising=False)
    monkeypatch.setenv("LLM_RPM", "0")
    monkeypatch.setitem(base.usage, "calls", 0)
    monkeypatch.setitem(base.usage, "tokens", 0)
    a = base.BaseAgent("role", model="fake/model")
    a.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()))
    a.slept = slept
    return a


def script(agent, *results):
    """Each call pops the next result: an Exception is raised, anything else is returned."""
    queue = list(results)

    def create_with_completion(**kw):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    agent.client.chat.completions.create_with_completion = create_with_completion


def test_success_records_usage(agent):
    script(agent, (OK, raw(321)))
    assert agent.analyze("ctx", AnalystOutput) == OK
    assert base.usage == {"calls": 1, "tokens": 321}


def test_rate_limit_waits_the_suggested_time_then_retries(agent):
    script(agent, Exception("RateLimitError: ... Please try again in 8.5s."), (OK, raw()))
    assert agent.analyze("ctx", AnalystOutput) == OK
    assert agent.slept == [9.5]


def test_rate_limit_with_minutes_parses(agent):
    script(agent, Exception("RateLimitError: try again in 1m20.5s"), (OK, raw()))
    agent.analyze("ctx", AnalystOutput)
    assert agent.slept == [81.5]


def test_daily_limit_fails_fast(agent):
    script(agent, Exception("RateLimitError: tokens per day (TPD) ... try again in 14m20s"))
    with pytest.raises(Exception, match="RateLimitError"):
        agent.analyze("ctx", AnalystOutput)
    assert agent.slept == []


def test_tool_use_failed_is_retried_without_sleeping(agent):
    script(agent, Exception("tool_use_failed: bad call"), (OK, raw()))
    assert agent.analyze("ctx", AnalystOutput) == OK
    assert agent.slept == []


def test_other_errors_propagate(agent):
    script(agent, ValueError("boom"))
    with pytest.raises(ValueError):
        agent.analyze("ctx", AnalystOutput)


def test_token_budget_reserves_and_waits(monkeypatch):
    monkeypatch.setenv("LLM_TPM", "1000")
    monkeypatch.setitem(base._tokens, "m1", base.deque())
    first = base._wait_for_tokens("m1", 800)  # fits
    assert first is not None and first[1] == 800
    slept = []

    def fake_sleep(s):  # pretend a minute passed so the window empties
        slept.append(s)
        base._tokens["m1"].clear()

    monkeypatch.setattr(base.time, "sleep", fake_sleep)
    base._wait_for_tokens("m1", 800)  # 800 + 800 > 1000 -> must wait
    assert len(slept) == 1
    base._record_tokens(first, 50)
    assert first[1] == 50  # estimate replaced by the real count


def test_token_budget_unlimited_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_TPM", raising=False)
    assert base._wait_for_tokens("m2", 10**6) is None
