#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
cd "$ROOT_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing .venv. Run: bash scripts/setup_venv.sh" >&2
  exit 2
fi

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/run_daily_report.py" "$@"
