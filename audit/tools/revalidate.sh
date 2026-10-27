#!/usr/bin/env bash
# Round-2 re-validation (Phase 7 checks): suites 3x, slow tests 5x, audit suite, Python 3.14, static checks,
# bandit, pip-audit, combined in-process coverage. Usage (repo root): audit/tools/revalidate.sh <out-file>
set -u
cd "$(dirname "$0")/../.." || exit 2
PY=.venv/bin/python
PY314=$HOME/.cache/certgen-audit/venv314/bin/python
out=${1:-audit/evidence/stage2/R2-P7-01-suites-static.txt}
q=(-q -p no:cacheprovider)
summary() { grep -E "^FAILED|^ERROR (audit|tests)/|[0-9]+ (passed|failed)" | sed "s/ - .*//"; }
{
  echo "# Round-2 re-validation (EXECUTED) $(date -u +%FT%TZ) HEAD $(git rev-parse --short HEAD), worktree audit/remediation"
  for i in 1 2 3; do echo "-- project suite run $i ($($PY -V))"; $PY -m pytest tests "${q[@]}" -rf 2>&1 | summary; done
  for i in 1 2 3 4 5; do echo "-- slow run $i"; $PY -m pytest tests -m slow "${q[@]}" -rf 2>&1 | summary; done
  echo "-- audit suite ($($PY -V))"; $PY -m pytest audit/tests "${q[@]}" -o timeout=200 -rf 2>&1 | summary
  echo "-- project suite ($($PY314 -V))"; $PY314 -m pytest tests "${q[@]}" -rf 2>&1 | summary
  echo "-- audit suite ($($PY314 -V))"; $PY314 -m pytest audit/tests "${q[@]}" -o timeout=200 -rf 2>&1 | summary
  echo "-- ruff check ."; .venv/bin/ruff check . 2>&1 | tail -1
  echo "-- ruff format --check ."; .venv/bin/ruff format --check . 2>&1 | tail -1
  echo "-- mypy --strict certgen"; .venv/bin/mypy --strict certgen 2>&1 | tail -1
  echo "-- bandit -r certgen"; .venv/bin/bandit -q -r certgen 2>&1 | grep -E "Issue|No issues|Low:|Medium:|High:" | sort | uniq -c
  echo "-- bandit -r scripts"; .venv/bin/bandit -q -r scripts 2>&1 | grep -E "Issue:" | sed 's/Location: //' | sort | uniq -c
  echo "-- pip-audit runtime"; .venv/bin/pip-audit -r requirements.txt 2>&1 | tail -2
  echo "-- pip-audit dev"; .venv/bin/pip-audit -r requirements-dev.txt 2>&1 | tail -2
  echo "-- combined in-process coverage (project + audit suites; subprocess servers not traced)"
  rm -f .coverage .coverage.*
  .venv/bin/coverage run -p --branch --source=certgen -m pytest tests "${q[@]}" >/dev/null 2>&1
  .venv/bin/coverage run -p --branch --source=certgen -m pytest audit/tests "${q[@]}" -o timeout=200 >/dev/null 2>&1
  .venv/bin/coverage combine -q && .venv/bin/coverage report -m 2>&1
  rm -f .coverage .coverage.*
} > "$out" 2>&1
cat "$out"
