# trading-agents

Four analysts (fundamental, technical, sentiment, news) study a stock, bull and bear researchers debate it, a trader and a three-way risk panel refine the plan, and a portfolio manager issues a Buy to Sell rating. An autonomous loop then screens a watchlist, sizes positions and sends orders to an Alpaca **paper** account, with a Rust terminal UI, Prometheus/Grafana metrics and Langfuse traces.

**The LLM proposes; plain Python decides:** a rule-based risk gate can veto any order, and the LLM cannot override it. Paper trading only. Not financial advice.
<img width="1436" height="1034" alt="image" src="https://github.com/user-attachments/assets/837709e7-3586-4473-ad05-814352e4408c" />
<img width="2173" height="1200" alt="image" src="https://github.com/user-attachments/assets/715fcf34-35f6-4d36-967f-2551d6382538" />



## How it works

```mermaid
flowchart TB
    classDef data fill:#e8f1ff,stroke:#3b7ddd,color:#0b2a55
    classDef analyst fill:#e6f7ec,stroke:#2e9e5b,color:#0b3d20
    classDef debate fill:#fff4d6,stroke:#d9a400,color:#4d3a00
    classDef manager fill:#f0e6ff,stroke:#7a4fd1,color:#2a0f5c
    classDef risk fill:#ffe6e6,stroke:#d64545,color:#5c0f0f
    classDef code fill:#e9ecef,stroke:#495057,color:#212529
    classDef out fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d20
    classDef obs fill:#fde8f3,stroke:#c2255c,color:#5c0f33

    subgraph DATA["Data (fails soft to 'no data')"]
        YF[(yfinance<br/>prices, fundamentals, news)]:::data
        ALT[(Reddit, StockTwits,<br/>FRED, Polymarket)]:::data
        MEM[(decisions.json<br/>past calls + realised return)]:::data
    end

    subgraph PIPE["LangGraph pipeline, one ticker"]
        FA[Fundamental<br/>analyst]:::analyst
        TA[Technical<br/>analyst]:::analyst
        SA[Sentiment<br/>analyst]:::analyst
        NA[News and macro<br/>analyst]:::analyst
        BULL[Bull<br/>researcher]:::debate
        BEAR[Bear<br/>researcher]:::debate
        RM[Research Manager<br/>deep model]:::manager
        TR[Trader<br/>buy / sell / hold plan]:::manager
        AGG[Aggressive<br/>risk analyst]:::risk
        CON[Conservative<br/>risk analyst]:::risk
        NEU[Neutral<br/>risk analyst]:::risk
        PM[Portfolio Manager<br/>deep model]:::manager
        RATING([Rating: Buy, Overweight,<br/>Hold, Underweight, Sell]):::out
    end

    YF --> FA & TA & SA & NA
    ALT --> SA & NA
    FA & TA & SA & NA -->|run in parallel, then join| BULL
    BULL <-->|debate rounds| BEAR
    BULL -->|debate over| RM
    BEAR -->|debate over| RM
    RM --> TR
    TR --> AGG --> CON --> NEU
    NEU --> PM
    MEM -->|last decision + return| PM
    PM --> RATING

    subgraph DAILY["Daily run: plain Python decides"]
        SCR[Screener<br/>watchlist + held, no LLM]:::code
        PLAN[Portfolio planner<br/>LLM: ratings to order intents]:::manager
        GATE{{Risk gate<br/>can veto, LLM cannot override}}:::risk
        BRK[Alpaca paper broker]:::out
        LOG[(trades.json<br/>orders + rejections)]:::data
    end

    SCR -->|top N tickers| FA
    RATING --> PLAN --> GATE
    GATE -->|approved| BRK --> LOG
    GATE -->|rejected + reason| LOG

    subgraph APP["App"]
        TUI[Rust TUI<br/>analyze, portfolio]:::code
        API[FastAPI :8000]:::code
    end

    TUI <--> API
    API --> SCR
    API -->|/metrics| PROM[Prometheus]:::obs --> GRAF[Grafana]:::obs
    PIPE -.->|traces| LF[Langfuse Cloud]:::obs
```

## Features
- 4 analysts, Bull/Bear debate, Research Manager, Trader, 3-way risk debate, Portfolio Manager; 5-tier rating (Buy / Overweight / Hold / Underweight / Sell).
- Structured (Pydantic) output from every agent.
- Decision log with reflection: the next run on a ticker sees the realised return of the last call.
- Autonomous paper portfolio: screen, analyse, plan, risk gate, execute, log. Dry run by default; orders need `--execute`.
- Risk gate: 10% max per stock, 10% cash floor, 5 positions, mandatory stop-loss, halt after a 10% drawdown from peak. Sells always pass.
- Optional weekday 16:30 ET schedule, and an equity-vs-SPY report (`python -m trading.report`).

## UI
Rust terminal app (ratatui). Keys: `a` Analyze (ticker input, live stage progress, coloured rating, reports, debates), `p` Portfolio (left half: equity, positions, recent runs, dry/execute triggers; right half: portfolio value over time on top, allocation pie chart below, needs a terminal 100+ columns wide), `q` quit. The raw-data tabs show prices, fundamentals and news.

## Built with
Python 3.11, LangGraph, instructor + LiteLLM (Gemini or Groq), FastAPI, yfinance, Alpaca (`alpaca-py`, paper), Rust + ratatui, Docker Compose, Prometheus, Grafana, OpenTelemetry, Langfuse.

## Observability
- **Traces** (Langfuse Cloud, free tier): one trace per analysis, a span per agent, prompts and replies. Set `LANGFUSE_*` in `core/.env`; `TRACE_CONTENT=0` hides the text.
- **Metrics**: the API serves `/metrics` (LLM calls, tokens, rate limits, node timings, portfolio, orders). Prometheus scrapes it; Grafana has a "Trading Agents" dashboard; alerts live in `observability/prometheus/alerts.yml`.

| Service    | URL                   | Login                    |
|------------|-----------------------|--------------------------|
| API        | http://localhost:8000 | none                     |
| Grafana    | http://localhost:3001 | admin / admin-local-pass |
| Prometheus | http://localhost:9090 | none                     |

## Run
Copy `core/.env.example` to `core/.env` and add `GEMINI_API_KEY` (or Groq), plus optional `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (paper) and `LANGFUSE_*`. Without Alpaca keys a fake $100k account is used.

```bash
./start.sh                                   # everything in Docker + the TUI; stops all on quit
docker compose up -d --build                 # background stack only
docker compose exec tui tui                  # open the TUI (the tui container is part of the stack)

# other features
docker compose run --rm api python run.py AAPL                              # analyse one stock
docker compose run --rm api python -m trading.daily_run --tickers AAPL      # dry run
docker compose run --rm api python -m trading.daily_run --tickers AAPL --execute   # sends PAPER orders
ENABLE_SCHEDULER=1 docker compose up -d      # daily cycle inside the API (dry run unless SCHEDULER_EXECUTE=1)
docker compose down                          # stop
```

Without Docker: `cd core && python3.11 -m venv venv && venv/bin/pip install -r requirements.txt`, then `venv/bin/python run.py AAPL`, or `cargo run` in `tui/` with the API on 127.0.0.1:8000.

## Test
`cd core && venv/bin/python -m pytest -q` (no network, no API key).
