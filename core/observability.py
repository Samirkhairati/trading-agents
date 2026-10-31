"""Metrics (Prometheus, scraped from /metrics) and traces (OpenTelemetry -> Langfuse).

Metrics are always collected (cheap, in-process). Tracing is off unless LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are
set; without them the OpenTelemetry API hands out no-op spans, so callers never need an `if`.
Nothing here may raise into the pipeline: exporters and setup fail soft.
"""
import base64
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry import context as otel_context, trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger(__name__)

# --- metrics (names are the contract used by observability/prometheus + grafana) --------------------------------
_LLM_BUCKETS = (0.5, 1, 2, 5, 10, 20, 40, 80, 160)
LLM_CALLS = Counter("llm_calls", "LLM calls", ["model", "status"])
LLM_TOKENS = Counter("llm_tokens", "Tokens used (prompt + completion)", ["model"])
LLM_SECONDS = Histogram("llm_call_seconds", "LLM call latency, excluding throttle waits", ["model"], buckets=_LLM_BUCKETS)
LLM_WAIT = Counter("llm_throttle_wait_seconds", "Seconds spent waiting on the local throttle or provider retry sleeps", ["model"])
LLM_RATE_LIMITS = Counter("llm_rate_limit_hits", "Provider rate-limit (429) responses", ["model"])
NODE_SECONDS = Histogram("node_seconds", "Graph node duration", ["node"], buckets=_LLM_BUCKETS + (320, 640))
ANALYSIS_RUNS = Counter("analysis_runs", "Single-ticker analysis runs", ["status"])
DAILY_RUNS = Counter("daily_run", "Paper-trading daily runs", ["status"])
DAILY_SECONDS = Histogram("daily_run_seconds", "Daily run duration", buckets=(30, 60, 120, 300, 600, 1200, 2400, 3600))
DAILY_LAST_OK = Gauge("daily_run_last_success_timestamp_seconds", "Unix time of the last successful daily run")
EQUITY = Gauge("portfolio_equity_usd", "Account equity")
CASH = Gauge("portfolio_cash_usd", "Account cash")
PEAK = Gauge("portfolio_peak_equity_usd", "Highest equity seen")
DRAWDOWN = Gauge("portfolio_drawdown_pct", "Percent below peak equity")
POSITIONS = Gauge("portfolio_positions", "Open positions")
ORDERS = Counter("orders", "Orders by outcome", ["result"])
ORDERS_REJECTED = Counter("orders_rejected", "Risk-gate rejections by rule", ["rule"])
BROKER_ERRORS = Counter("broker_errors", "Broker calls that raised")


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


# --- tracing -----------------------------------------------------------------------------------------------------
_tracer = trace.get_tracer("trading-agents")
_initialised = False


def init_tracing() -> bool:
    """Send spans to Langfuse over OTLP if its keys are configured. Idempotent; returns whether tracing is on."""
    global _initialised
    if _initialised:
        return True
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        host = (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or "http://localhost:3000").rstrip("/")
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{host}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"},
            timeout=10,
        )
        provider = TracerProvider(resource=Resource.create({"service.name": "trading-agents"}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _set_tracer(trace.get_tracer("trading-agents"))
        _initialised = True
        logger.info("tracing to Langfuse at %s", host)
    except Exception as e:  # observability must never stop the app
        logger.warning("tracing disabled: %s", e)
    return _initialised


def _set_tracer(t) -> None:
    global _tracer
    _tracer = t


def _short(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, default=str)


@contextmanager
def span(name: str, kind: str = "span", **attrs) -> Iterator[trace.Span]:
    """Open a span that becomes the parent of spans created inside. kind: span | generation | agent (Langfuse types)."""
    with _tracer.start_as_current_span(name) as s:
        s.set_attribute("langfuse.observation.type", kind)
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else _short(v))
        yield s


def record_generation(s: trace.Span, model: str, messages: list, output, prompt_tokens: int, completion_tokens: int) -> None:
    """Fill in a generation span with the LLM payload (set TRACE_CONTENT=0 to drop prompts and replies)."""
    s.set_attribute("gen_ai.request.model", model)
    s.set_attribute("gen_ai.usage.input_tokens", int(prompt_tokens or 0))
    s.set_attribute("gen_ai.usage.output_tokens", int(completion_tokens or 0))
    if os.environ.get("TRACE_CONTENT", "1") != "0":
        s.set_attribute("langfuse.observation.input", _short(messages))
        s.set_attribute("langfuse.observation.output", _short(output.model_dump(mode="json") if hasattr(output, "model_dump") else output))


def tag_trace(s: trace.Span, name: str, ticker: Optional[str] = None, **meta) -> None:
    """Name the whole Langfuse trace from its root span."""
    s.set_attribute("langfuse.trace.name", name)
    if ticker:
        s.set_attribute("langfuse.trace.tags", [ticker])
    for k, v in meta.items():
        s.set_attribute(f"langfuse.trace.metadata.{k}", v if isinstance(v, (str, int, float, bool)) else _short(v))


def shutdown() -> None:
    """Flush pending spans (short-lived CLI runs would otherwise exit before the batch exporter sends them)."""
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush:
        try:
            flush(5000)
        except Exception:
            pass


@contextmanager
def timed(hist: Histogram, *labels: str) -> Iterator[None]:
    t = time.monotonic()
    try:
        yield
    finally:
        (hist.labels(*labels) if labels else hist).observe(time.monotonic() - t)


@contextmanager
def use_context(ctx) -> Iterator[None]:
    """Run a block under a captured OpenTelemetry context (graph nodes execute in worker threads)."""
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def traced_node(name: str, fn, ctx=None, **attrs):
    """Wrap a graph node: parent its span under `ctx` (default: the context current right now) and time it."""
    parent = ctx if ctx is not None else otel_context.get_current()

    def wrapper(state):
        with use_context(parent), span(f"node.{name}", kind="agent", **attrs), timed(NODE_SECONDS, name):
            return fn(state)

    wrapper.__name__ = getattr(fn, "__name__", name)
    return wrapper
