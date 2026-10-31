# Observability (metrics)

Prometheus and Grafana are part of the root `docker-compose.yml` (start everything with `docker compose up -d --build` from the repo root).
Everything binds to 127.0.0.1. Traces are not here: they go to Langfuse Cloud (`LANGFUSE_*` in `core/.env`).

| Service    | URL                   | Login                      |
|------------|-----------------------|----------------------------|
| Grafana    | http://localhost:3001 | admin / admin-local-pass   |
| Prometheus | http://localhost:9090 | none                       |

Prometheus scrapes the `api` container (`api:8000/metrics`, every 15s, job `trading-agents`).
Alert rules (`prometheus/alerts.yml`) show in Prometheus and Grafana only; notifications would need Alertmanager.
Reset metrics data: `docker compose down -v`.
