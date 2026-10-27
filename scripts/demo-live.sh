#!/usr/bin/env bash
# Use the interpreter directly: this also works after moving the project folder.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo 'First run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt' >&2
  exit 1
fi
exec .venv/bin/python scripts/live_demo.py "$@"
