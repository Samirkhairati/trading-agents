# trading-agents

A small, runnable model of [TradingAgents](TradingAgents/) (the reference repo; read-only, git-ignored, never edit).
Same idea, far less machinery: a team of LLM agents analyses one stock and a Portfolio Manager issues a rating.
Python backend (`core/`) + Rust ratatui frontend (`tui/`).

**Goal: it works end to end and is easy to run and test. Not: cover every edge case.**

## Scope rules (read before writing code)

Keep, from TradingAgents:
- Pipeline: 4 analysts -> Bull/Bear debate -> Research Manager -> Trader -> 3-way risk debate -> Portfolio Manager.
- 5-tier rating: Buy / Overweight / Hold / Underweight / Sell.
- Structured (Pydantic) outputs for every agent, via `instructor` + LiteLLM.
- A simple decision log with reflection on realised return.
- LangGraph as the orchestrator (kept on purpose), but only plain nodes + conditional edges.

Drop (do NOT add): tool-calling ReAct loops (analysts get pre-fetched data), multiple LLM provider clients,
Alpha Vantage / vendor routing, symbol normalisation, crypto mode, SQLite checkpointing, i18n, Docker, CI,
caching layers, retry frameworks, config classes. No new dependencies unless a chunk says so.
Prefer 20 plain lines over one abstraction. Match the existing style (type hints, short docstrings, few comments).

## Environment

- Python 3.11 venv at `core/venv` (git-ignored). Always use `core/venv/bin/python`. Run everything from `core/`.
- `core/.env` holds `GEMINI_API_KEY` (never print, log or commit it). `core/.env.example` documents the vars.
- Env vars: `GEMINI_API_KEY`, `LLM_MODEL` (default `gemini/gemini-3.5-flash`),
  optional `LLM_DEEP_MODEL` (Research Manager + Portfolio Manager; defaults to `LLM_MODEL`).
- Gemini free tier = ~20 requests/day PER MODEL. A run costs 8 + 2*DEBATE_ROUNDS calls (~2-4 min). `core/.env` uses `LLM_MODEL=gemini/gemini-3.1-flash-lite` (10 calls) + `LLM_DEEP_MODEL=gemini/gemini-3.5-flash` (2 calls) + `DEBATE_ROUNDS=1` => ~2 runs/day on lite, 10 on 3.5. `gemini-3.8-flash` hung once; check model names with the ListModels REST call, they change often.
- Groq free tier (alternative, see `.env.example`): per model 8K tokens/min and 200K tokens/day; a full analysis is ~57K tokens (~8 min with `LLM_TPM=7000`), so ~3 runs/day/model. `qwen3.8-27b` also has an output-tokens/min cap and failed; use the gpt-oss models.
- Local model option: Ollama in Docker on the Oracle VM (`ssh oracle-vm`, standalone container `ollama` on docker network `llm`, never part of the compose stack, 127.0.0.1:11434, model `qwen3:4b-instruct-2507-q4_K_M`), reached through
  `ssh -fN -L 11434:127.0.0.1:11434 oracle-vm`. `core/.env` sets `LLM_MODEL=ollama_chat/...`, `LLM_MODE=json_schema`, `LLM_TIMEOUT`, `LLM_MAX_TOKENS`. ~6 tokens/s on 2 ARM cores:
  one analysis takes 10-20 min, so test with one ticker and never run two jobs at once (the API returns 409). The VM also hosts other projects: keep the container's `--memory 7g`.
- Account: default `PORTFOLIO_MODE=sim` = `trading/sim.py` persistent simulated account (supports admin deposits/sells); state dir = `STATE_DIR` (Docker volume `trading-agents_state` on the server). Keep every new persistent file under `paths.state_dir()`.
- CI/CD: `.github/workflows/deploy.yml` (test -> GHCR image -> SSH deploy). Server public host: trading-agents.samirkhairati.me (Caddy, `./deploy.sh public`).
- Server deploy: `./deploy.sh` (+ `public` / `tunnel`), overlay `docker-compose.vm.yml`; public hosts are sslip.io names added to `/opt/caddy/Caddyfile` (backup `.bak`); public UI is read-only (non-GET blocked in Caddy).
- Web UI: `core/web/` (vanilla JS, served by the API at `/`) replaced the TUI (`tui/` is kept but no longer in compose and cannot start jobs: the public API is read-only except `POST /api/queue`). Owner page `/admin` (`core/admin.py`, `ADMIN_PASSWORD` in core/.env, never mentioned in the public JS/HTML; a test enforces that). Jobs: `core/jobs.py` (one worker, visitor queue capped at QUEUE_MAX=3, scheduler + admin bypass the cap). `core/activity.py` = live state for `/api/live`,
  `core/history.py` = one JSON per finished analysis in `core/data/analyses/` (git-ignored) for `/api/analyses`.
- Data: yfinance for prices, fundamentals, news (required). Reddit / StockTwits / FRED / Polymarket are optional extras:
  any failure must degrade to a "no data" string, never crash a run, and must not block for more than ~10s.
- Rust: `tui/` (`cargo run` from there). Backend on `http://127.0.0.1:8000`.

## Target layout (`core/`, absolute imports, no relative imports)

```
core/
  data/fetcher.py              yfinance: prices(+indicators), fundamentals, news
  data/alternative_fetchers.py optional sources, return strings, fail soft
  data/validator.py            stale-data guard (called by the price fetcher)
  agents/base.py               BaseAgent.analyze(context, ResponseModel)
  agents/schemas.py            ALL pydantic models
  agents/{fundamental,technical,sentiment,news,debate,risk,manager,trader}.py
  graph/state.py               AgentState TypedDict
  graph/trading_graph.py       build_graph() + run_analysis(ticker, date, on_progress)
  graph/conditional_logic.py   debate / risk loop routing
  memory.py                    JSON decision log + reflection context
  main.py                      FastAPI app
  run.py                       CLI: `python run.py AAPL` -> prints the full report
  tests/                       pytest, LLM mocked, no network
start.sh                       boots backend + TUI
```

## Roadmap (Phase 1: single-ticker analysis - chunks 1-6 DONE; Phase 2 below)

Work in chunks, in order. Each chunk ends with its **Verify** step passing, then a commit.
Never start a chunk on top of a failing earlier verify. Report what you actually ran and saw.

### Chunk 1 - Make `core` importable and one agent runnable
- `schemas.py`: add the models agents already import or the pipeline needs: `DebateOutput` (argument),
  `RiskOutput` (risk_assessment, max_position_size as % of portfolio, risk_score 1-10),
  `TraderProposal` (action: TraderAction, reasoning, entry/target/stop optional floats),
  `TradingDecision` (rating: PortfolioRating, target_price, stop_loss, justification). Fix `AnalystOutput` use
  (field is `reasoning`, not `summary`).
- Fix every import to absolute (`from agents.base import ...`). Remove `from ..x`.
- `fetch_price_history` -> returns a DataFrame with indicators; add `format_price_report(df)` returning the text for the
  Technical Analyst (last ~30 rows, key indicators). Call `assert_not_stale`. Delete the redundant `indicators.py` or make it
  the single place for the indicator text, not both.
- Cap network waits: Reddit retries <= 2 with <= 3s backoff; every urlopen has a timeout.
- Fix method names/signatures so each agent has one method taking one context string (or its documented args).
- Add `requirements.txt` entries actually used (add `pytest`). Add `core/.env.example` var list.
- **Verify:** `cd core && venv/bin/python -c "import agents.manager, agents.debate, agents.risk, graph.trading_graph"` succeeds;
  `venv/bin/python test.py` (rewrite as a small smoke script) runs the Technical Analyst on AAPL against Gemini and prints score + report.

### Chunk 2 - LangGraph pipeline (the core of the project)
- `state.py`: AgentState with ticker, date, 4 analyst reports, `debate` (history list, count, bull/bear text),
  `investment_plan` (Research Manager), `trader_proposal`, `risk` (history, count, latest_speaker), `final_decision`, `past_context`.
  Use plain keys; lists accumulate by returning the full new list from the node (read old, append, return) or `operator.add` reducers.
  Make sure debate/risk state is MERGED, never overwritten (current code loses `count`/`history` fields).
- Nodes: Fundamental, Technical, Sentiment, News/Macro analysts (fetch data inside the node, wrap fetch in try/except -> "no data" text;
  an analyst failure must not abort the run) -> Bull <-> Bear (N rounds, default 2 each, `MAX_DEBATE_ROUNDS` constant) ->
  Research Manager (deep model, produces `ResearchPlan`) -> Trader (`TraderProposal`) ->
  Aggressive -> Conservative -> Neutral (one pass each, `MAX_RISK_ROUNDS`) -> Portfolio Manager (deep model, `TradingDecision`).
- Analysts may run sequentially (simple, like TradingAgents). Optional: fan them out in parallel with LangGraph branches if trivial.
- `run_analysis(ticker, date=None, on_progress=None) -> dict` compiles the graph, streams node updates
  (`graph.stream(..., stream_mode="updates")`), calls `on_progress(stage_name, done_count, total)` after each node,
  returns the final state as a JSON-serialisable dict. Do NOT create a module-level global `app` (clashes with FastAPI).
- Conditional routing returns only valid node names; pass an explicit path map (TradingAgents bug #1088).
- `run.py`: CLI printing each stage as it finishes and a final report (rating, target, stop, justification).
- **Verify:** `venv/bin/python run.py AAPL` completes with a final rating; `venv/bin/python run.py ZZZZNOTREAL` fails with a clear
  one-line error, not a traceback. Keep one run under ~3 minutes (small prompts, trim data to what the agent needs).

### Chunk 3 - Memory and reflection (TradingAgents' `memory.py` + `reflection.py`, simplified)
- `memory.py`: append-only `core/data/decisions.json` (git-ignored): ticker, date, rating, entry_price, target, stop, justification.
- Before the Portfolio Manager runs: look up the last decision for the ticker, fetch current price, compute realised return,
  produce a short `past_context` string ("Last time (2025-01-02) you said Buy at $180; stock is now $195, +8.3%. ...").
  Include a one-sentence lessons prompt only if cheap; no extra LLM call is required.
- After the run: append the new decision. Expose `get_history(ticker)`.
- **Verify:** run `run.py AAPL` twice; the second run's output shows the past-decision context was passed to the Portfolio Manager.

### Chunk 4 - Job API
- `main.py`: keep `/api/data/*` endpoints (TUI raw-data tabs use them). Replace the disabled stubs with:
  - `POST /api/analyze {ticker, date?}` -> `{job_id}`; runs `run_analysis` in a thread via BackgroundTasks / executor.
  - `GET /api/status/{job_id}` -> `{state: running|done|error, stage, done, total, message}`.
  - `GET /api/result/{job_id}` -> final dict (404 if unknown, 409 if not done). `GET /api/history/{ticker}` -> past decisions.
- Jobs live in an in-memory dict. Errors set `state=error` with a short message. Bind `127.0.0.1`.
- **Verify:** with the server up, `curl -X POST .../api/analyze -d '{"ticker":"AAPL"}'`, poll status until `done`, fetch result.

### Chunk 5 - TUI analysis mode (`tui/src/main.rs`; split into modules if it grows past ~600 lines)
- Add an Analyze mode alongside the existing raw-data tabs: key to toggle. Ticker input -> POST /api/analyze ->
  poll /api/status every ~1s via a tokio task + mpsc channel -> render a stage progress list (done / current / pending) ->
  on done fetch /api/result and render: big colored rating (Buy/Overweight green, Hold yellow, Underweight/Sell red),
  target, stop, justification, and tabs for each analyst report / debate / risk views (scrollable).
- Errors render in red; `Esc` returns to input. The backend being down shows a clear message, not a panic.
- **Verify:** `cargo build` clean (no warnings if cheap); run backend + TUI, analyse AAPL, see result. (Use a screenshot/terminal read.)

### Chunk 6 - Convenience, tests, docs
- `start.sh` (root, executable): start uvicorn in background, wait for it to respond, `cargo run`, kill backend on exit (trap).
- `core/tests/` with pytest, **no network, no real LLM**: (1) indicator/price text from a synthetic DataFrame,
  (2) schema validation + rating enum, (3) debate/risk routing returns valid node names and terminates,
  (4) full graph run with `BaseAgent.analyze` monkeypatched to return canned models -> final decision present,
  (5) memory round trip in a tmp path, (6) API job lifecycle via FastAPI TestClient with the pipeline monkeypatched.
- `README.md`: 15 lines max: setup, `.env`, how to run CLI / API / TUI, how to run tests.
- Un-ignore `instructions.md`/`overview.md` only if the user asks; do not delete them.
- **Verify:** `cd core && venv/bin/python -m pytest -q` is green; `./start.sh` works from a clean shell.

## Phase 2 - Paper-trading portfolio manager (chunks 7-12)

Chunks 1-6 are done (single-ticker analysis). Phase 2 turns it into an autonomous **paper** portfolio.
Paper trading only (Alpaca `paper=True`); never wire live trading. Core rule, as in real LLM-trading systems:
**the LLM proposes, plain Python decides and executes.** The risk gate can veto anything and the LLM cannot override it.
Dry-run is the default; sending orders needs an explicit `--execute`.

New package `core/trading/` (absolute imports, same style as the rest):
```
trading/types.py       dataclasses: Account, Position, ProposedOrder, Rejection, RiskConfig (shared interface, already written)
trading/broker.py      Broker protocol; AlpacaBroker (alpaca-py, paper); FakeBroker (in-memory, used by tests + dry-run)
trading/risk.py        check_orders(orders, account, positions, cfg, peak_equity) -> (approved, rejected); pure functions
trading/screener.py    pick_candidates(watchlist, held, ...) -> tickers to analyse (no LLM)
trading/planner.py     PortfolioAgent (LLM): ratings + account + positions -> PortfolioPlan (list of order intents)
trading/daily_run.py   the loop: screen -> analyse -> plan -> risk gate -> execute -> log. CLI: --dry-run (default) / --execute
trading/state.py       data/trades.json (decision+order log) and data/portfolio_state.json (peak equity, last-analysed dates)
watchlist.json         ~20 liquid tickers (core/watchlist.json)
```
Env: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (paper keys; never print or commit). Tests use `FakeBroker`, no network, no LLM.

Risk defaults (`RiskConfig`): max 10% of equity per position, keep >=10% cash, max 5 positions, every buy must carry a stop-loss
(and take-profit), stop must be below entry for longs, minimum order $50, halt all new buys if equity is >=10% below its peak.
Sells/closes are always allowed (the gate never blocks reducing risk).

### Chunk 7 - Broker layer
`Broker` protocol: `get_account() -> Account`, `get_positions() -> list[Position]`, `get_price(symbol) -> float`,
`place_order(ProposedOrder) -> str` (bracket order when stop/take given; notional-based market buy),
`close_position(symbol) -> str`. `AlpacaBroker` (alpaca-py TradingClient(paper=True), StockHistoricalDataClient for prices) and
`FakeBroker(cash, prices)` that fills instantly at its price dict and tracks positions. **Verify:** unit tests on FakeBroker;
AlpacaBroker constructed with fake keys makes no network call at import/construct time; with real paper keys `python -m trading.broker`
prints account + positions.

### Chunk 8 - Risk gate
Pure functions, heavily unit-tested (this is the most important module). Each rejection has a human-readable reason.
**Verify:** pytest covers every rule, plus "sells always pass", "halt on drawdown", "cash floor applies across multiple orders in one batch".

### Chunk 9 - Screener, planner, daily run
- Screener: always include held tickers; score watchlist by |5-day move|, volume spike vs 20d avg, headline count; skip tickers analysed in the
  last `ANALYSE_COOLDOWN_DAYS` (default 3) unless held; return top N (default 3). No LLM.
- Planner: one LLM call (deep model) with cash/positions/ratings/past trades -> `PortfolioPlan(orders=[intent(symbol, action buy|sell|hold, weight_pct, stop_loss, take_profit, reason)], summary)`.
  Convert to `ProposedOrder` in code (weight_pct * equity -> notional; stop/take default to entry -/+ 2xATR-ish percentages if the LLM omits them).
- `daily_run.py`: orchestrates, prints a readable table, logs every decision and order (and every rejection with reason) to `trades.json`.
- **Verify:** `python -m trading.daily_run --dry-run` with FakeBroker and mocked LLM in tests; one real dry-run against Alpaca paper + Gemini.

### Chunk 10 - Scheduling and API
- `python -m trading.daily_run --schedule` uses APScheduler: weekdays 16:30 US/Eastern (after close). Skips weekends/holidays via broker clock if available.
- API: `GET /api/portfolio` (account + positions + peak/drawdown), `GET /api/trades` (log, newest first), `POST /api/daily-run?execute=false` (job like /api/analyze, reuse the jobs dict).
- **Verify:** TestClient tests with FakeBroker.

### Chunk 11 - TUI portfolio dashboard
New mode (key `p`): equity/cash/P&L header, positions table (symbol, qty, avg entry, price, P&L %), recent trades/decisions with reasons and
rejections, key to trigger a dry-run and one to trigger an execute run (confirmation required). Same style/module split as `analyze.rs`.

### Chunk 12 - Evaluation and docs
- `trading/report.py`: compare portfolio equity curve (from state log) vs buy-and-hold SPY since start; print return, max drawdown, win rate.
- README section: set paper keys, run dry-run, run schedule, read the report. Explicit note: paper results over weeks prove little; not financial advice.

## Observability (done)
`core/observability.py`: Prometheus metrics (`/metrics`) + OpenTelemetry spans to Langfuse Cloud (off unless `LANGFUSE_*` keys are set).
`observability/` is an opt-in Docker Compose stack for metrics only (Prometheus :9090, Grafana :3001, alerts in `prometheus/alerts.yml`);
metric names are a contract between `observability.py` and the dashboard/alerts, change both together. The API refreshes portfolio gauges
every 5 min; `ENABLE_SCHEDULER=1` also runs the weekday daily cycle in-process. Never put prompts/keys in span attributes beyond `record_generation`.

## Conventions
- Commit only when the user asks; then one commit per chunk (message: what works now). Never commit `.env`, `venv/`, `target/`, `decisions.json`.
- Anything that calls Gemini costs money: smoke tests use a single ticker, short prompts, and the cheap model.
- When blocked on a decision that is the user's (API key, model choice, scope), ask; otherwise pick the simple option and note it.
- Final reports to the user: what ran, what the output was, what is not verified.

## Demo mode and portfolio charts
`DEMO_PORTFOLIO=1` (core/.env) swaps the broker for a seeded FakeBroker (`trading/demo.py`, state in `core/data/demo`, git-ignored) so the TUI value chart / allocation pie, Grafana gauges and a labelled Langfuse trace have data. Never touches Alpaca. The TUI container idles; open it with `docker compose exec tui tui` (`start.sh` does this). The OTel tracer provider is private on purpose: the global one makes FastAPI trace every /metrics scrape into Langfuse.
