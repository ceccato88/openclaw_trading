#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  uv venv --python 3.12 "$BASE_DIR/.venv"
  uv pip install --python "$VENV_PYTHON" -U hyperliquid-python-sdk eth-account python-dotenv requests
fi

exec "$VENV_PYTHON" "$BASE_DIR/scripts/smoke_test_openclaw.py" "$@"
