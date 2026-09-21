#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p runtime
.venv/bin/python -m backend.worker > runtime/worker.log 2>&1 &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM
.venv/bin/python -m uvicorn backend.api:app --host 127.0.0.1 --port 3090
