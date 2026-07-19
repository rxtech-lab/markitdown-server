#!/usr/bin/env bash
#
# End-to-end smoke test against a locally running api + worker.
#
# Submits a real PDF, polls the job to completion, then walks every page back
# out and checks the content actually survived. This exercises the whole path
# the k8s benchmark does — download, hash, chunk, claim, convert, assemble,
# paginate — without needing a cluster.
#
# Usage:
#   make up
#   make api      # in another shell
#   make worker   # in a third shell
#   make smoke
set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
API_KEY="${ADMIN_API_KEY:-dev-key}"
# Small enough to finish quickly, large enough to split into several chunks at
# the default 20 pages/chunk.
PDF_URL="${PDF_URL:-https://raw.githubusercontent.com/GSimas/Asimov/master/Books/I_Robot/I%20Robot.pdf}"
TIMEOUT="${TIMEOUT:-300}"

fail() { echo "FAIL: $*" >&2; exit 1; }

curl -sf "${BASE_URL}/" >/dev/null 2>&1 \
  || fail "api is not reachable at ${BASE_URL} (run 'make api')"

ready="$(curl -sS "${BASE_URL}/readyz" 2>/dev/null)"
echo "$ready" | grep -q '"status": *"ok"' \
  || fail "dependencies are not ready (run 'make up'): $ready"

echo "submitting ${PDF_URL}"
submit="$(curl -sS -w '\n%{http_code}' \
  -X POST "${BASE_URL}/async/convert" \
  -H "x-api-key: ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d "{\"file\": \"${PDF_URL}\"}")"

code="$(printf '%s' "$submit" | tail -n1)"
payload="$(printf '%s' "$submit" | sed '$d')"

case "$code" in
  200)
    echo "cache hit — already converted. 'make reset' to start clean."
    printf '%s' "$payload" | python3 -m json.tool | head -20
    exit 0
    ;;
  202) ;;
  *) fail "submit returned HTTP ${code}: $(printf '%s' "$payload" | head -c 400)" ;;
esac

job_id="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
doc_key="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["doc_key"])')"
chunks="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total_chunks"])')"
echo "job ${job_id} queued as ${chunks} chunk(s)"

start=$(date +%s)
deadline=$(( start + TIMEOUT ))
last=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  job="$(curl -sS "${BASE_URL}/convert/jobs/${job_id}" -H "x-api-key: ${API_KEY}")"
  status="$(printf '%s' "$job" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null)"
  progress="$(printf '%s' "$job" | python3 -c \
    'import json,sys; p=json.load(sys.stdin)["progress"]; print(f"{p[\"completed\"]}/{p[\"total\"]} ({p[\"percent\"]}%)")' 2>/dev/null)"

  if [ "$progress" != "$last" ]; then
    echo "  ${status}: ${progress}"
    last="$progress"
  fi

  case "$status" in
    done) break ;;
    failed)
      fail "job failed: $(printf '%s' "$job" | python3 -c 'import json,sys; print(json.load(sys.stdin)["error"])')"
      ;;
  esac
  sleep 2
done

[ "$status" = "done" ] || fail "job did not finish within ${TIMEOUT}s (last status: ${status:-unknown}). Is 'make worker' running?"

elapsed=$(( $(date +%s) - start ))
echo "converted in ${elapsed}s"

# Walk every page back out, so a job that reports done but stored nothing
# readable still fails the smoke test.
page1="$(curl -sS "${BASE_URL}/convert/${doc_key}/pages/1" -H "x-api-key: ${API_KEY}")"
total_pages="$(printf '%s' "$page1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pagination"]["total_pages"])' 2>/dev/null)" \
  || fail "could not read page 1: $(printf '%s' "$page1" | head -c 300)"
total_length="$(printf '%s' "$page1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pagination"]["total_length"])')"

chars=0
for page in $(seq 1 "$total_pages"); do
  body="$(curl -sS "${BASE_URL}/convert/${doc_key}/pages/${page}" -H "x-api-key: ${API_KEY}")"
  n="$(printf '%s' "$body" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["content"]))' 2>/dev/null)" \
    || fail "page ${page} of ${total_pages} was not readable"
  chars=$(( chars + n ))
done

echo "read ${total_pages} page(s), ${chars} chars (manifest says ${total_length})"
[ "$chars" -gt 0 ] || fail "document is empty"

# Re-submitting identical bytes must hit the cache and queue nothing.
echo "checking the content-addressed cache"
again_code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/async/convert" \
  -H "x-api-key: ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d "{\"file\": \"${PDF_URL}\"}")"
[ "$again_code" = "200" ] || fail "expected a 200 cache hit on resubmit, got ${again_code}"

echo
echo "PASS: converted in ${elapsed}s, ${total_pages} pages, cache hit on resubmit"
