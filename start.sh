#!/bin/bash
# One command: builds and starts the API + Prometheus + Grafana in Docker, opens the TUI, and stops everything when you quit it.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
trap 'docker compose down' EXIT
docker compose up -d --build --wait
docker compose exec tui tui
