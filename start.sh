#!/bin/bash
# One command: opens the SSH tunnel to the local-LLM host (if LLM_MODEL is an ollama model), builds and starts the API + web UI
# + Prometheus + Grafana in Docker, opens http://localhost:8000, and stops the stack on Ctrl+C.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
if grep -q '^LLM_MODEL="\?ollama' core/.env 2>/dev/null && ! nc -z localhost 11434 2>/dev/null; then
  echo "opening SSH tunnel to ${OLLAMA_SSH_HOST:-oracle-vm} (Ollama :11434)"
  ssh -fN -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 11434:127.0.0.1:11434 "${OLLAMA_SSH_HOST:-oracle-vm}"
fi
trap 'docker compose down' EXIT
docker compose up -d --build --wait
echo "web UI: http://localhost:8000   (Ctrl+C stops everything)"
command -v open >/dev/null && open http://localhost:8000
docker compose logs -f api
