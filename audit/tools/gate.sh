#!/usr/bin/env bash
# Stage 2 regression gate: project suite, ruff, mypy, full audit suite.
# Usage (from the repository root): audit/tools/gate.sh <label>   -> audit/evidence/stage2/<label>-gate.txt
set -u
cd "$(dirname "$0")/../.." || exit 2
PY=.venv/bin/python
label=${1:-gate}
mkdir -p audit/evidence/stage2
out=audit/evidence/stage2/$label-gate.txt
{
  echo "== $label $(date -u +%FT%TZ) HEAD $(git rev-parse --short HEAD) python $($PY -V 2>&1)"
  echo "-- pytest tests"; $PY -m pytest tests -q -p no:cacheprovider 2>&1 | tail -1
  echo "-- ruff check ."; .venv/bin/ruff check . 2>&1 | tail -1
  echo "-- ruff format --check ."; .venv/bin/ruff format --check . 2>&1 | tail -1
  echo "-- mypy --strict certgen"; .venv/bin/mypy --strict certgen 2>&1 | tail -1
  echo "-- pytest audit/tests"
  $PY -m pytest audit/tests -q -p no:cacheprovider -o timeout=200 -rf 2>&1 | grep -E "^FAILED|^ERROR (audit|tests)/|[0-9]+ (passed|failed)" | sed 's/ - .*//'
} > "$out" 2>&1
cat "$out"
