#!/bin/bash
# Boots the Python backend, then the Rust TUI; stops the backend when the TUI exits.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT/core"
./venv/bin/python main.py > "$ROOT/core/backend.log" 2>&1 &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null' EXIT

echo "Waiting for backend..."
for _ in $(seq 1 30); do
  curl -s -o /dev/null http://127.0.0.1:8000/docs && break
  kill -0 $BACKEND_PID 2>/dev/null || { echo "Backend failed to start:"; tail -20 "$ROOT/core/backend.log"; exit 1; }
  sleep 1
done

cd "$ROOT/tui"
cargo run --quiet
