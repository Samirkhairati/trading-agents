#!/bin/bash
# Run the stack (API + web UI + daily scheduler + Prometheus/Grafana) on a server over SSH. Ollama is NOT part of it: it runs
# separately on the server as a container named `ollama` (see README) and the api reaches it over a Docker network.
#   ./deploy.sh            sync code + config, build and start everything on the server (the CI workflow does pull-and-restart instead)
#   ./deploy.sh public     put https://trading-agents.samirkhairati.me and https://grafana.<ip>.sslip.io behind the server's Caddy
#   ./deploy.sh tunnel     forward the server's UI/Grafana to localhost:8000 / :3001 (Ctrl+C closes it)
#   ./deploy.sh logs | status | down
# Persistent data lives in the Docker volume `trading-agents_state`; deploying never touches it (`down` keeps it too).
set -e
HOST="${DEPLOY_HOST:-oracle-vm}"
DIR="${DEPLOY_DIR:-trading-agents}"   # relative to the remote home
DOMAIN="${APP_DOMAIN:-trading-agents.samirkhairati.me}"
cd "$(cd "$(dirname "$0")" && pwd)"
remote() { ssh "$HOST" "cd $DIR && $*"; }

case "${1:-deploy}" in
  tunnel) echo "UI http://localhost:8000   Grafana http://localhost:3001   (Ctrl+C to close)"
          exec ssh -N -o ServerAliveInterval=30 -L 8000:127.0.0.1:8000 -L 3001:127.0.0.1:3001 "$HOST" ;;
  logs)   remote docker compose logs -f --tail 100 api; exit ;;
  status) remote "docker compose ps && curl -s localhost:8000/api/schedule && echo && curl -s localhost:8000/api/live | head -c 400 && echo"; exit ;;
  down)   remote docker compose down; exit ;;
  public) IP=$(ssh "$HOST" "curl -s -m5 ifconfig.me" | tr . -); GF="grafana.$IP.sslip.io"
          ssh "$HOST" "python3 - '$DOMAIN' '$GF'" <<'PY'
import re, subprocess, sys
domain, grafana = sys.argv[1:3]
path = "/opt/caddy/Caddyfile"
text = open(path).read()
open(path + ".bak", "w").write(text)
text = re.sub(r"\n?# BEGIN trading-agents.*?# END trading-agents\n?", "\n", text, flags=re.S)
text = re.sub(r"\ntrading\.[0-9-]+\.sslip\.io \{.*\Z", "\n", text, flags=re.S)  # the first, unmarked version of these blocks
block = f"""
# BEGIN trading-agents
{domain} {{
	encode gzip
	handle /metrics* {{
		respond 404
	}}
	handle {{
		reverse_proxy 127.0.0.1:8000
	}}
}}

{grafana} {{
	encode gzip
	reverse_proxy 127.0.0.1:3001
}}
# END trading-agents
"""
open(path, "w").write(text.rstrip("\n") + "\n" + block)
run = lambda *a: subprocess.run(["docker", "exec", "caddy", "caddy", *a, "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], check=True, capture_output=True)
try:
    run("validate")
except subprocess.CalledProcessError as e:
    open(path, "w").write(open(path + ".bak").read())  # never leave the other sites broken
    sys.exit("Caddyfile invalid, restored: " + e.stderr.decode()[-300:])
run("reload")
PY
          echo "UI      https://$DOMAIN"; echo "Grafana https://$GF   (anonymous viewers; admin password = GRAFANA_ADMIN_PASSWORD in ~/$DIR/.env on the server)"; exit ;;
esac

ssh "$HOST" "mkdir -p $DIR"
rsync -az --exclude='.git' --exclude='venv' --exclude='target' --exclude='TradingAgents' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='.DS_Store' --exclude='*.log' --exclude='/core/.env' --exclude='/.env' --exclude='/state' ./ "$HOST:$DIR/"

# Keys: send core/.env without the keys this setup does not use (a local model needs none of the hosted-LLM keys)
if grep -q '^LLM_MODEL="\?ollama' core/.env; then DROP='^(OPENAI|ANTHROPIC|GROQ|GEMINI)_API_KEY='; else DROP='^(OPENAI|ANTHROPIC)_API_KEY='; fi
grep -vE "$DROP" core/.env | ssh "$HOST" "umask 077; cat > $DIR/core/.env"
IP=$(ssh "$HOST" "curl -s -m5 ifconfig.me" | tr . -)
ssh "$HOST" "cd $DIR && pw=\$(grep '^GRAFANA_ADMIN_PASSWORD=' .env 2>/dev/null | cut -d= -f2); pw=\${pw:-\$(openssl rand -hex 12)}
  printf 'COMPOSE_FILE=docker-compose.yml:docker-compose.vm.yml\nOLLAMA_API_BASE=http://ollama:11434\nGRAFANA_ANONYMOUS=true\nGRAFANA_ROOT_URL=https://grafana.$IP.sslip.io\nGRAFANA_ADMIN_PASSWORD=%s\n' \"\$pw\" > .env; chmod 600 .env"

# Ollama is separate: it must already run on the server as a container named `ollama`; this only puts it on the shared network
ssh "$HOST" "docker inspect ollama >/dev/null 2>&1" || { echo "no container named 'ollama' on $HOST: start it first (README: Local model)"; exit 1; }
ssh "$HOST" "docker network inspect llm >/dev/null 2>&1 || docker network create llm >/dev/null; docker network connect llm ollama 2>/dev/null || true"

remote docker compose up -d --build --remove-orphans --wait --wait-timeout 900
remote 'docker compose exec -T grafana grafana cli admin reset-admin-password "$(grep ^GRAFANA_ADMIN_PASSWORD= .env | cut -d= -f2)" >/dev/null'
remote docker compose ps
echo
echo "Deployed. Public URL (after './deploy.sh public'): https://$DOMAIN   Or: ./deploy.sh tunnel"
