#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
if [[ -n "${ASHARE_PYTHON:-}" ]]; then
  PYTHON_BIN="$ASHARE_PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi
exec "$PYTHON_BIN" "$ROOT_DIR/scripts/run_daily_report.py" "$@"
