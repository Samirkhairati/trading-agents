# Observability stack (local dev)

`docker compose up -d` from this directory. Everything binds to 127.0.0.1.

| Service    | URL                   | Login                                   |
|------------|-----------------------|-----------------------------------------|
| Langfuse   | http://localhost:3000 | admin@local.dev / admin-local-pass      |
| Grafana    | http://localhost:3001 | admin / admin-local-pass                |
| Prometheus | http://localhost:9090 | none                                    |

Langfuse is headless-initialised (org `local-org`, project `trading-agents`); sign-up is disabled.
Prometheus scrapes the backend on the host (`host.docker.internal:8000/metrics`, every 15s, job `trading-agents`).
Alert rules (`prometheus/alerts.yml`) show in Prometheus and Grafana only; notifications would need Alertmanager.

App env vars (placeholders, local only):
```
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-local-trading
LANGFUSE_SECRET_KEY=sk-lf-local-trading
```
OTLP traces: POST http://localhost:3000/api/public/otel/v1/traces (Basic auth with the keys above, header `x-langfuse-ingestion-version: 4`).

Stop: `docker compose down`. Reset all data (volumes): `docker compose down -v`.
