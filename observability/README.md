# Observability stack (local dev)

Metrics only: `docker compose up -d` from this directory starts Prometheus and Grafana. Everything binds to 127.0.0.1.
Traces are not in here: they go to Langfuse Cloud (set `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` in `core/.env`).

| Service    | URL                   | Login                      |
|------------|-----------------------|----------------------------|
| Grafana    | http://localhost:3001 | admin / admin-local-pass   |
| Prometheus | http://localhost:9090 | none                       |

Prometheus scrapes the backend on the host (`host.docker.internal:8000/metrics`, every 15s, job `trading-agents`).
Alert rules (`prometheus/alerts.yml`) show in Prometheus and Grafana only; notifications would need Alertmanager.

Stop: `docker compose down`. Reset all data (volumes): `docker compose down -v`.
