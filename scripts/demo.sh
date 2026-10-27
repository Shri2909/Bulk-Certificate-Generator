#!/usr/bin/env bash
# Live demo of certgen: run it from the repository root and narrate each step.
#
#   scripts/demo.sh          pause before each step (press Enter to continue)
#   scripts/demo.sh --auto   no pauses (rehearsal)
#
# Uses its own port (8010) and data dir (data/demo), so it never touches a server you already run.
# Renders are slowed to 400 ms each with the test-only CERTGEN_TEST_SLOW_RENDER_MS knob, purely so
# progress and the crash recovery are visible to the audience. Real renders take a few milliseconds.

set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
PORT=8010
BASE="http://127.0.0.1:$PORT"
DATA=data/demo
AUTO=0
[[ "${1:-}" == "--auto" ]] && AUTO=1

if [[ ! -x "$PY" ]]; then
  echo "Run the README setup first (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)." >&2
  exit 1
fi
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }

BOLD=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; RED=$'\e[31m'; CYAN=$'\e[36m'; RESET=$'\e[0m'
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null; done
  chmod -R u+rwX "$DATA" 2>/dev/null
}
trap cleanup EXIT

step() {
  echo
  echo "${BOLD}${CYAN}━━ $1 ━━${RESET}"
  [[ -n "${2:-}" ]] && echo "${DIM}$2${RESET}"
  if [[ $AUTO -eq 0 ]]; then read -r -p "${DIM}[Enter]${RESET} " _; fi
}

show() {  # print a command the script runs itself on the next line
  echo "${GREEN}\$ $*${RESET}"
}

run() {  # show the command, then run it
  echo "${GREEN}\$ $*${RESET}"
  eval "$@"
}

json() { "$PY" -m json.tool --no-ensure-ascii; }
field() { "$PY" -c "import sys, json; d = json.load(sys.stdin); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }

progress() {  # live progress bar until the job is terminal
  local job=$1
  while true; do
    local line
    line=$(curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$job" | "$PY" -c '
import sys, json
d = json.load(sys.stdin)
c, p = d["counts"], d["progress"]
n = int(p * 30)
bar = "[" + "#" * n + "." * (30 - n) + "]"
detail = " ".join(f"{k}={c[k]}" for k in ("succeeded", "processing", "pending", "failed", "rejected"))
print(d["status"], bar, format(p, ".0%"), detail)')
    printf "\r  %-110s" "$line"
    case "$line" in queued*|processing*) sleep 0.3 ;; *) echo; break ;; esac
  done
}

count() {  # items of a job in a given status
  curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$1/items?status=$2&limit=1000" | field 'len(d["items"])'
}

start_worker() {
  "$PY" -m certgen.worker 2>>"$DATA/worker.log" &
  PIDS+=($!)
  LAST_WORKER=$!
}

# ------------------------------------------------------------------------------------------------

rm -rf "$DATA" && mkdir -p "$DATA"
export CERTGEN_DATA_DIR=$DATA CERTGEN_PORT=$PORT CERTGEN_LEASE_SECONDS=3 \
       CERTGEN_RETRY_DELAY_SECONDS=0.5 CERTGEN_POLL_INTERVAL_SECONDS=0.1 CERTGEN_TEST_SLOW_RENDER_MS=400

step "1. Start the API (workers run as separate processes in this demo)" \
     "One command, no broker: SQLite + files in $DATA. Embedded workers are off so we can kill one later."
show "CERTGEN_EMBEDDED_WORKER=false $PY -m certgen &"
CERTGEN_EMBEDDED_WORKER=false "$PY" -m certgen > "$DATA/api.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 100); do curl -s "$BASE/healthz" >/dev/null && break; sleep 0.1; done
# Every /v1 call needs the API token the server created in its data dir on first start.
TOKEN=$(cat "$DATA/api-token")
AUTH="Authorization: Bearer $TOKEN"
run "curl -s $BASE/healthz | json"

cat > "$DATA/job.json" <<'EOF'
{
  "certificate": {"title": "Python for Data Engineering", "issuer": "Acme Academy", "issued_on": "2026-10-01"},
  "recipients": [
    {"name": "Zoë Łukasz", "email": "zoe@example.com"},
    {"name": "Νίκος Παπαδόπουλος", "email": "nikos@example.org"},
    {"name": "Екатерина Иванова", "email": "ekaterina@example.org"},
    {"name": "Seán O'Connor-Smith", "email": "sean@example.ie"},
    {"name": "Bob", "email": "not-an-email"},
    {"name": "李小龙", "email": "bruce@example.com"},
    {"name": "Zoe Again", "email": " ZOE@example.com "},
    {"name": "Anne-Marie Lévesque", "email": "anne@example.ca"},
    {"name": "Mohammed Al-Rashid", "email": "mohammed@example.com"},
    {"name": "Priya Raman", "email": "priya@example.in"},
    {"name": "Ngọc Trần", "email": "ngoc@example.vn"},
    {"name": "Lars Øvergaard", "email": "lars@example.no"}
  ]
}
EOF

step "2. Dry run: validate the whole batch without storing anything" \
     "Bad email, Chinese characters the font cannot draw, and a case-insensitive duplicate are reported per row."
run "curl -s -H \"\$AUTH\" -X POST '$BASE/v1/certificate-jobs?dry_run=true' -H 'Content-Type: application/json' --data @$DATA/job.json | json"

step "3. Submit for real, with an Idempotency-Key" \
     "202 immediately: valid rows are queued, invalid rows are stored as rejected. Nothing is rendered yet."
run "curl -s -H \"\$AUTH\" -X POST $BASE/v1/certificate-jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-order-1' --data @$DATA/job.json -o $DATA/created.json -w '%{http_code}\n' | tail -1"
JOB=$(field 'd["id"]' < "$DATA/created.json")
"$PY" -c "import json; d = json.load(open('$DATA/created.json')); print('job', d['id'], d['status'], d['counts']); [print('  row', e['row_index'], e['code'], '-', e['message']) for e in d['errors']]"

step "4. The client timed out and sends the same request again" \
     "Same key + same body: 200 replay of the same job, no duplicate."
run "curl -s -H \"\$AUTH\" -o /dev/null -D - -X POST $BASE/v1/certificate-jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-order-1' --data @$DATA/job.json | grep -iE '^HTTP|^location|^idempotent'"

step "5. Start a worker and watch progress" \
     "The worker claims one item at a time in a single locked transaction, renders it, writes it atomically."
show "$PY -m certgen.worker &   # worker A"
start_worker; WORKER_A=$LAST_WORKER
while [[ $(count "$JOB" succeeded) -lt 3 ]]; do sleep 0.1; done
curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$JOB" | field '"  progress so far: " + str(d["counts"])'

step "6. Crash: kill -9 worker A in the middle of a certificate" \
     "Its item stays 'processing' with a 3 s lease. Nobody has to notice the crash."
while [[ $(count "$JOB" processing) -lt 1 ]]; do sleep 0.02; done
run "kill -9 $WORKER_A"
wait "$WORKER_A" 2>/dev/null
curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$JOB/items?status=processing" | "$PY" -c "import sys, json; [print('  stuck item: row', i['row_index'], i['name'], 'attempts', i['attempts']) for i in json.load(sys.stdin)['items']]"

step "7. Start worker B: it finishes the job and takes over the crashed item when the lease expires" \
     "A new random token fences out the dead worker. Same certificate id, same file, no duplicate."
show "$PY -m certgen.worker &   # worker B"
start_worker
progress "$JOB"
curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$JOB/items?status=succeeded&limit=1000" | "$PY" -c "import sys, json; [print('  row', i['row_index'], 'attempts', i['attempts'], '<- reclaimed after the crash' if i['attempts'] > 1 else '') for i in json.load(sys.stdin)['items']]"
echo "  PDF files on disk: $(find "$DATA/certificates" -name '*.pdf' | wc -l)   temp files: $(find "$DATA/certificates" -name '*.tmp' | wc -l)"

step "8. Results: which rows failed and why, one PDF, and the whole job as a ZIP"
run "curl -s -H \"\$AUTH\" '$BASE/v1/certificate-jobs/$JOB/items?status=rejected' | field '[(i[\"row_index\"], i[\"name\"], i[\"errors\"][0][\"code\"]) for i in d[\"items\"]]'"
CERT=$(curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$JOB/items?status=succeeded&limit=1" | field 'd["items"][0]["certificate_id"]')
run "curl -s -H \"\$AUTH\" -o $DATA/certificate.pdf $BASE/v1/certificates/$CERT && file $DATA/certificate.pdf"
run "curl -s -H \"\$AUTH\" -o $DATA/certificates.zip $BASE/v1/certificate-jobs/$JOB/archive && $PY -m zipfile -l $DATA/certificates.zip"
run "$PY -c \"import zipfile; print(zipfile.ZipFile('$DATA/certificates.zip').read('manifest.csv').decode())\""
if [[ $AUTO -eq 0 ]] && command -v xdg-open >/dev/null; then xdg-open "$DATA/certificate.pdf" >/dev/null 2>&1 & fi

step "9. A failure that is not the recipient's fault: the disk becomes read-only" \
     "Each item is retried, then marked failed with a safe message. Other jobs and items are unaffected."
run "chmod 500 $DATA/certificates"
run "curl -s -H \"\$AUTH\" -X POST $BASE/v1/certificate-jobs -H 'Content-Type: application/json' -d '{\"certificate\": {\"title\": \"Retry Demo\", \"issuer\": \"Acme Academy\", \"issued_on\": \"2026-10-01\"}, \"recipients\": [{\"name\": \"Grace Hopper\", \"email\": \"grace@example.org\"}, {\"name\": \"Alan Turing\", \"email\": \"alan@example.org\"}]}' -o $DATA/retry-job.json -w '%{http_code}\n'"
JOB2=$(field 'd["id"]' < "$DATA/retry-job.json")
progress "$JOB2"
curl -s -H "$AUTH" "$BASE/v1/certificate-jobs/$JOB2/items?status=failed" | "$PY" -c "import sys, json; [print('  row', i['row_index'], i['name'], 'attempts', i['attempts'], i['errors'][0]['code'], '-', i['errors'][0]['message']) for i in json.load(sys.stdin)['items']]"

step "10. Fix the disk, then retry only the failed certificates" \
     "One call re-queues them; they keep their certificate ids."
run "chmod 700 $DATA/certificates"
run "curl -s -H \"\$AUTH\" -X POST $BASE/v1/certificate-jobs/$JOB2/retry | field 'd[\"status\"], d[\"counts\"]'"
progress "$JOB2"

step "11. Shut down gracefully" "Workers finish the item in hand; nothing is lost."
cleanup
PIDS=()
echo "${BOLD}Demo complete.${RESET} Files are in $DATA (delete it any time). API docs: run 'python -m certgen' and open http://127.0.0.1:8000/docs"
