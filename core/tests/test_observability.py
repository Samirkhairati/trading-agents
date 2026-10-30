"""Metrics and traces: values are read from the Prometheus registry and an in-memory span exporter. No network."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

import agents.base as base
import observability as obs
from agents.schemas import AnalystOutput, PlanIntent, PortfolioPlan
from graph.trading_graph import TOTAL_STAGES, run_analysis
from trading.broker import FakeBroker
from trading.daily_run import run_daily


def val(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(obs, "_tracer", provider.get_tracer("test"))
    return exporter


def test_metrics_endpoint_exposes_contract_names():
    import main

    text = TestClient(main.app).get("/metrics").text
    for name in ["llm_calls_total", "llm_tokens_total", "llm_call_seconds_bucket", "llm_throttle_wait_seconds_total",
                 "llm_rate_limit_hits_total", "node_seconds_bucket", "analysis_runs_total", "daily_run_total",
                 "daily_run_seconds_bucket", "daily_run_last_success_timestamp_seconds", "portfolio_equity_usd",
                 "portfolio_drawdown_pct", "orders_total", "orders_rejected_total", "broker_errors_total"]:
        # labelled series only appear after first use, so match the family declaration
        assert f"# TYPE {name.rsplit('_bucket', 1)[0].removesuffix('_total')}" in text or name in text, name


def test_graph_spans_share_one_trace_and_nest_under_analysis(fake_llm, fake_data, spans):
    before = val("analysis_runs_total", status="ok")
    run_analysis("fake")
    finished = spans.get_finished_spans()
    root = next(s for s in finished if s.name == "analysis")
    nodes = [s for s in finished if s.name.startswith("node.")]
    assert len(nodes) == TOTAL_STAGES
    assert {s.context.trace_id for s in nodes} == {root.context.trace_id}  # also true for the parallel analysts
    assert all(s.parent.span_id == root.context.span_id for s in nodes)
    assert root.attributes["langfuse.trace.name"] == "analysis FAKE"
    assert val("analysis_runs_total", status="ok") == before + 1
    assert val("node_seconds_count", node="Trader") >= 1


def test_generation_span_and_llm_metrics(spans, monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_RPM", "0")
    monkeypatch.delenv("LLM_TPM", raising=False)
    agent = base.BaseAgent("role", model="fake/m")
    answers = [Exception("RateLimitError: try again in 2s"),
               (AnalystOutput(score=7, reasoning="r"), SimpleNamespace(usage=SimpleNamespace(total_tokens=150, prompt_tokens=100, completion_tokens=50)))]

    def create_with_completion(**kw):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create_with_completion=create_with_completion)))
    ok, rl, tok = val("llm_calls_total", model="fake/m", status="ok"), val("llm_rate_limit_hits_total", model="fake/m"), val("llm_tokens_total", model="fake/m")
    agent.analyze("some context", AnalystOutput)
    assert val("llm_calls_total", model="fake/m", status="ok") == ok + 1
    assert val("llm_rate_limit_hits_total", model="fake/m") == rl + 1
    assert val("llm_tokens_total", model="fake/m") == tok + 150
    gen = next(s for s in spans.get_finished_spans() if s.name == "llm.BaseAgent")
    assert gen.attributes["langfuse.observation.type"] == "generation"
    assert gen.attributes["gen_ai.request.model"] == "fake/m"
    assert gen.attributes["gen_ai.usage.input_tokens"] == 100 and gen.attributes["gen_ai.usage.output_tokens"] == 50
    assert "some context" in gen.attributes["langfuse.observation.input"]


def test_trace_content_can_be_disabled(spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "0")
    with obs.span("g", kind="generation") as s:
        obs.record_generation(s, "m", [{"role": "user", "content": "secret"}], "out", 1, 1)
    attrs = spans.get_finished_spans()[0].attributes
    assert "langfuse.observation.input" not in attrs and attrs["gen_ai.usage.input_tokens"] == 1


def test_failed_llm_call_counts_as_error(spans, monkeypatch):
    agent = base.BaseAgent("role", model="fake/err")
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create_with_completion=lambda **kw: (_ for _ in ()).throw(ValueError("boom")))))
    before = val("llm_calls_total", model="fake/err", status="error")
    with pytest.raises(ValueError):
        agent.analyze("c", AnalystOutput)
    assert val("llm_calls_total", model="fake/err", status="error") == before + 1


def test_daily_run_updates_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    broker = FakeBroker(cash=100_000, prices={"AAA": 100.0})
    plan = lambda ctx: PortfolioPlan(summary="s", intents=[PlanIntent(symbol="AAA", action="buy", target_weight_pct=50, reason="r")])
    decision = {"rating": "Buy", "target_price": 120.0, "stop_loss": 90.0, "position_size": 5.0, "justification": "x"}
    ok, approved, executed = val("daily_run_total", status="ok"), val("orders_total", result="approved"), val("orders_total", result="executed")
    run_daily(broker, execute=True, tickers=["AAA"], analyse=lambda t: decision, plan_fn=plan, log=lambda *_: None)
    assert val("daily_run_total", status="ok") == ok + 1
    assert val("orders_total", result="approved") == approved + 1
    assert val("orders_total", result="executed") == executed + 1
    assert val("daily_run_last_success_timestamp_seconds") > 0
    assert val("portfolio_equity_usd") == pytest.approx(100_000, rel=1e-3)
    assert val("portfolio_positions") == 1


def test_daily_run_failure_counts_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    before = val("daily_run_total", status="error")
    boom = lambda ctx: (_ for _ in ()).throw(RuntimeError("llm down"))
    with pytest.raises(RuntimeError):
        run_daily(FakeBroker(prices={"AAA": 100.0}), tickers=["AAA"],
                  analyse=lambda t: {"rating": "Buy", "target_price": 1, "stop_loss": 1, "position_size": 1, "justification": ""},
                  plan_fn=boom, log=lambda *_: None)
    assert val("daily_run_total", status="error") == before + 1
