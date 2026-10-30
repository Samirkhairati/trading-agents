# SimpleTradingAgents

A small model of [TradingAgents](TradingAgents/): analysts -> bull/bear debate -> research manager -> trader -> risk debate -> portfolio manager, orchestrated with LangGraph. Python backend (`core/`) + Rust TUI (`tui/`).

## Setup
```bash
cd core && python3.11 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY
```

## Run
- CLI: `cd core && venv/bin/python run.py AAPL`
- API + TUI: `./start.sh` (needs `cargo`)
- Smoke test one agent: `cd core && venv/bin/python test.py AAPL`

## Test (no network, no API key)
`cd core && venv/bin/python -m pytest -q`

Decisions are logged to `core/data/decisions.json`; the next run on the same ticker gets the realised return as context.

## Paper trading (Phase 2)
Alpaca paper keys go in `core/.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Without them a fake in-memory $100k account is used.
```bash
cd core
venv/bin/python -m trading.daily_run --tickers AAPL   # dry run: screen/analyse/plan/risk-gate, sends nothing
venv/bin/python -m trading.daily_run --execute        # sends bracket orders to the PAPER account
venv/bin/python -m trading.daily_run --schedule       # weekdays 16:30 US/Eastern
venv/bin/python -m trading.report                     # equity vs SPY buy-and-hold
```
The LLM only proposes; `trading/risk.py` can veto any order (10% max per stock, 10% cash floor, 5 positions, required stop, drawdown halt).
Paper results over a few weeks prove little. Not financial advice.
Free Gemini tier is limited (about 5 requests/minute, ~20/day per model): `LLM_RPM=5` throttles calls; a run costs ~10 calls per ticker.

## Observability (optional)
`cd observability && docker compose up -d` starts Langfuse (traces, http://localhost:3000), Prometheus (:9090) and Grafana (dashboard, http://localhost:3001). Logins and details: `observability/README.md`.
- Traces: set `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in `core/.env` (defaults in `.env.example`); each analysis becomes a trace with a span per agent and every prompt/reply. `TRACE_CONTENT=0` hides the text.
- Metrics: the API serves `/metrics` (LLM calls, tokens, rate limits, node timings, portfolio, orders). Alerts are defined in `observability/prometheus/alerts.yml`.
- Unattended daily run: `ENABLE_SCHEDULER=1 ./start.sh` runs the weekday 16:30 ET cycle inside the API (dry run unless `SCHEDULER_EXECUTE=1`) so Prometheus always has a live target.
