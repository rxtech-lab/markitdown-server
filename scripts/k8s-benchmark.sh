#!/usr/bin/env bash
#
# Convert real Asimov Foundation PDFs through the service running in the
# cluster, enforcing a per-book time and memory budget.
#
# The budgets are regression guards, not performance targets: they are sized to
# catch the failure modes that actually matter (chunking silently falling back
# to whole-document conversion, or peak memory creeping back toward the pod
# limit) without failing on normal runner variance.
#
# Memory comes from the pod's cgroup high-water mark rather than `kubectl top`,
# which samples on an interval and routinely misses the spike that matters.
# That mark is cumulative and the cgroup is mounted read-only, so it cannot be
# reset in place -- the deployment is restarted before each book instead, which
# both gives a true per-book peak and makes every book start from a cold pod.
#
# Expects: kubectl context pointing at the cluster and $ADMIN_API_KEY set. The
# port-forward is managed here, since restarting the pod tears it down.
set -uo pipefail

NAMESPACE="${NAMESPACE:-markitdown-server}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
BASE_URL="http://localhost:${LOCAL_PORT}"
# Scales every time budget at once, for slower runners or local use.
BUDGET_SCALE="${BUDGET_SCALE:-1.0}"
# Fail if peak memory exceeds this share of the pod's limit.
MEM_BUDGET_PCT="${MEM_BUDGET_PCT:-90}"
# Per-book pod logs land here; the workflow uploads the directory as an artifact.
LOG_DIR="${LOG_DIR:-/tmp/benchmark-pod-logs}"
RAW="https://raw.githubusercontent.com/GSimas/Asimov/master/Books"

# name|url|budget_seconds
#
# Budgets are ~2.5x the time measured on a native CI runner at 2 CPU / 2
# workers (I Robot 12.9s, Prelude 21.0s, Foundation and Earth 71.4s, Trilogy
# 32.5s). That leaves room for runner variance while still catching a gross
# regression such as chunking silently falling back to whole-document
# conversion. Tighten further once several runs have established the spread.
#
# Ordered smallest -> largest so a failure surfaces on a cheap case first.
BOOKS=(
  "I Robot.pdf|${RAW}/I_Robot/I%20Robot.pdf|35"
  "Prelude to Foundation.pdf|${RAW}/Prelude_Foundation/Prelude%20to%20Foundation.pdf|55"
  # Slower than the Trilogy despite having half the pages: its pages take
  # markitdown's expensive form-detection path rather than the fast pdfminer
  # one, costing ~4x per page.
  "Foundation and Earth.pdf|${RAW}/Foundation_Earth/Foundation%20and%20Earth.pdf|180"
  "Foundation Trilogy.pdf|${RAW}/Foundation/Foundation%20Trilogy.pdf|85"
)

PF_PID=""

stop_port_forward() {
  [ -z "$PF_PID" ] && return 0
  kill "$PF_PID" 2>/dev/null
  # Reap it, so bash job control does not print "Terminated" over the report.
  wait "$PF_PID" 2>/dev/null
  PF_PID=""
}
trap stop_port_forward EXIT

# Convert a Kubernetes memory quantity to MiB. The API server normalises what
# the manifest says, so a limit written as "2048Mi" reads back as "2Gi" -- any
# parser that only handles one suffix silently produces nonsense percentages.
to_mib() {
  awk -v q="$1" 'BEGIN{
    if      (q ~ /Ki$/) { sub(/Ki$/,"",q); printf "%.0f", q/1024 }
    else if (q ~ /Mi$/) { sub(/Mi$/,"",q); printf "%.0f", q }
    else if (q ~ /Gi$/) { sub(/Gi$/,"",q); printf "%.0f", q*1024 }
    else if (q ~ /Ti$/) { sub(/Ti$/,"",q); printf "%.0f", q*1048576 }
    else if (q ~ /k$/)  { sub(/k$/,"",q);  printf "%.0f", q*1000/1048576 }
    else if (q ~ /M$/)  { sub(/M$/,"",q);  printf "%.0f", q*1000000/1048576 }
    else if (q ~ /G$/)  { sub(/G$/,"",q);  printf "%.0f", q*1000000000/1048576 }
    else if (q ~ /^[0-9.]+$/) { printf "%.0f", q/1048576 }
    else { printf "0" }
  }'
}

# Serving pods only.
#
# `--field-selector=status.phase=Running` is NOT enough: a pod being deleted
# stays in phase Running for the whole termination grace period (120s here), so
# after a rollout restart the outgoing pod keeps being counted long after the
# new one is serving. Filter on deletionTimestamp and readiness instead.
running_pod_names() {
  kubectl get pods -n "$NAMESPACE" -l app=markitdown-server -o json 2>/dev/null \
    | jq -r '.items[]
             | select(.metadata.deletionTimestamp == null)
             | select(.status.phase == "Running")
             | select(any(.status.containerStatuses[]?; .ready))
             | .metadata.name'
}

# Wait for exactly one serving pod, so before/after samples cannot straddle two
# different pods and report a phantom restart. Returns non-zero (and prints
# nothing) if the set never settles -- callers must treat that as an error, not
# as a zero reading.
pod_name() {
  local names count
  for _ in $(seq 1 90); do
    names="$(running_pod_names)"
    count="$(printf '%s\n' "$names" | grep -c .)"
    if [ "$count" = "1" ]; then
      printf '%s' "$names"
      return 0
    fi
    sleep 2
  done
  return 1
}

# An OOM kill restarts the container in place, keeping the pod name, so the
# restart counter is the signal that actually catches it.
restart_count() {
  kubectl get pod -n "$NAMESPACE" "$1" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null
}

peak_mb() {
  kubectl exec -n "$NAMESPACE" "$1" -- sh -c \
    'cat /sys/fs/cgroup/memory.peak 2>/dev/null \
     || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null \
     || echo 0' 2>/dev/null | awk '{printf "%.0f", $1/1048576}'
}

start_port_forward() {
  stop_port_forward
  kubectl port-forward -n "$NAMESPACE" svc/markitdown-server \
    "${LOCAL_PORT}:8080" >/dev/null 2>&1 &
  PF_PID=$!
  for _ in $(seq 1 30); do
    curl -sf "${BASE_URL}/" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "ERROR: service did not become reachable on ${BASE_URL}"
  return 1
}

slugify() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9' '-'
}

# Capture logs for a book now: the next book restarts the pod, which discards
# them. Grabbing logs only at the end would report the last book's pod no
# matter which one actually failed.
#
# Logs every matching pod rather than just the one we tracked -- when pod
# resolution is what failed, the logs are exactly what explains why, and
# reporting "(no logs captured)" in that case is the least useful outcome.
capture_logs() {
  local slug="$1" pod i=0
  while IFS= read -r pod; do
    [ -z "$pod" ] && continue
    i=$((i + 1))
    kubectl logs -n "$NAMESPACE" "$pod" --tail=200 \
      > "${LOG_DIR}/${slug}.${i}-${pod}.log" 2>&1 || true
    # A restarted container's interesting output is in the dead instance.
    kubectl logs -n "$NAMESPACE" "$pod" --previous --tail=200 \
      > "${LOG_DIR}/${slug}.${i}-${pod}.previous.log" 2>/dev/null || true
    [ -s "${LOG_DIR}/${slug}.${i}-${pod}.previous.log" ] \
      || rm -f "${LOG_DIR}/${slug}.${i}-${pod}.previous.log"
  done <<< "$(kubectl get pods -n "$NAMESPACE" -l app=markitdown-server \
                -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"
}

restart_pod() {
  kubectl rollout restart deployment/markitdown-server -n "$NAMESPACE" >/dev/null
  kubectl rollout status deployment/markitdown-server -n "$NAMESPACE" \
    --timeout=300s >/dev/null || return 1
  start_port_forward
}

MEM_LIMIT_RAW=$(kubectl get deployment markitdown-server -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null)
MEM_LIMIT_MB=$(to_mib "$MEM_LIMIT_RAW")
[ -z "$MEM_LIMIT_MB" ] && MEM_LIMIT_MB=0

mkdir -p "$LOG_DIR"

# The benchmark assumes a single replica: with more than one pod behind the
# service, the request and the cgroup read can land on different pods and every
# per-book number becomes meaningless. An active HPA breaks that assumption the
# moment conversion saturates the CPU, so fail up front with the actual reason
# rather than timing out later waiting for the pod set to settle.
HPA_MAX="$(kubectl get hpa markitdown-server-hpa -n "$NAMESPACE" \
  -o jsonpath='{.spec.maxReplicas}' 2>/dev/null)"
if [ -n "$HPA_MAX" ] && [ "$HPA_MAX" -gt 1 ]; then
  echo "ERROR: HorizontalPodAutoscaler allows up to ${HPA_MAX} replicas."
  echo "       It will scale up under conversion load and invalidate the"
  echo "       measurements. Delete it before benchmarking:"
  echo "         kubectl delete hpa markitdown-server-hpa -n ${NAMESPACE}"
  exit 1
fi

echo "namespace=$NAMESPACE  memory_limit=${MEM_LIMIT_MB}Mi (${MEM_LIMIT_RAW})  budget_scale=${BUDGET_SCALE}"
echo "(pod restarted before each book, so PEAK_MB is that book's own high-water mark)"
echo
printf '%-30s %7s %8s %8s %8s %9s  %s\n' \
  BOOK SECONDS BUDGET PEAK_MB LIMIT_PCT CHARS VERDICT
printf '%s\n' "-----------------------------------------------------------------------------------------"

failures=0
FAILED_BOOKS=()
for entry in "${BOOKS[@]}"; do
  IFS='|' read -r name url budget <<< "$entry"
  budget=$(awk -v b="$budget" -v s="$BUDGET_SCALE" 'BEGIN{printf "%.0f", b*s}')
  slug="$(slugify "$name")"

  if ! restart_pod; then
    printf '%-30s %7s %8s %8s %8s %9s  %s\n' \
      "$name" "-" "$budget" "-" "-" "-" "FAIL pod did not come up"
    capture_logs "$slug"
    FAILED_BOOKS+=("$slug|$name")
    failures=$((failures + 1))
    continue
  fi

  # Never default an unreadable value to zero: that turns "could not measure"
  # into a confident wrong number, which is how the phantom OOM verdicts got
  # reported. Bail out loudly instead.
  if ! pod="$(pod_name)" || [ -z "$pod" ]; then
    printf '%-30s %7s %8s %8s %8s %9s  %s\n' \
      "$name" "-" "$budget" "-" "-" "-" "FAIL could not resolve a single serving pod"
    kubectl get pods -n "$NAMESPACE" -l app=markitdown-server 2>&1 | head -10
    capture_logs "$slug"
    FAILED_BOOKS+=("$slug|$name")
    failures=$((failures + 1))
    continue
  fi
  restarts_before="$(restart_count "$pod")"
  [ -z "$restarts_before" ] && restarts_before=0

  start=$(date +%s.%N)
  # Cap the request just past the budget: a hang should fail on the budget, not
  # sit until the job-level timeout kills the whole run.
  max_time=$(awk -v b="$budget" 'BEGIN{printf "%.0f", b*1.5+60}')
  body="$(curl -sS --max-time "$max_time" -w '\n%{http_code}' \
    -X POST "${BASE_URL}/convert" \
    -H "x-api-key: ${ADMIN_API_KEY}" \
    -H 'Content-Type: application/json' \
    -d "{\"file\": \"${url}\"}" 2>&1)"
  end=$(date +%s.%N)

  status="$(printf '%s' "$body" | tail -n1)"
  payload="$(printf '%s' "$body" | sed '$d')"
  seconds="$(awk -v a="$start" -v b="$end" 'BEGIN{printf "%.1f", b-a}')"

  pod_after="$(pod_name)" || pod_after=""
  restarts_after="$(restart_count "$pod_after")"
  [ -z "$restarts_after" ] && restarts_after=0
  peak="$(peak_mb "$pod_after")"
  # A failed read must stay distinct from a genuine low number. The pod always
  # uses some memory, so 0 here means the cgroup read did not work.
  if [ -z "$peak" ] || [ "$peak" = "0" ]; then
    peak="?"
  fi

  verdict="ok"
  if [ -z "$pod_after" ]; then
    verdict="FAIL lost track of the serving pod after the request"
    failures=$((failures + 1))
  elif [ "$pod_after" != "$pod" ] || [ "$restarts_after" != "$restarts_before" ]; then
    # The peak reading is meaningless here: a container restart zeroes the
    # cgroup high-water mark, so a low number does not mean low usage.
    verdict="FAIL container restarted mid-request (likely OOM kill)"
    failures=$((failures + 1))
  elif [ "$peak" = "?" ]; then
    verdict="FAIL could not read peak memory from the pod cgroup"
    failures=$((failures + 1))
  elif [ "$status" != "200" ]; then
    verdict="FAIL http=$status"
    failures=$((failures + 1))
  elif [ "$(awk -v s="$seconds" -v b="$budget" 'BEGIN{print (s>b)?1:0}')" = "1" ]; then
    verdict="FAIL over time budget"
    failures=$((failures + 1))
  fi

  pct="-"
  if [ "$MEM_LIMIT_MB" != "0" ] && [ "$peak" != "?" ]; then
    raw_pct="$(awk -v p="$peak" -v l="$MEM_LIMIT_MB" 'BEGIN{printf "%.0f", 100*p/l}')"
    if [ "$verdict" = "ok" ] && \
       [ "$(awk -v p="$raw_pct" -v m="$MEM_BUDGET_PCT" 'BEGIN{print (p>m)?1:0}')" = "1" ]; then
      verdict="FAIL over memory budget (>${MEM_BUDGET_PCT}%)"
      failures=$((failures + 1))
    fi
    pct="${raw_pct}%"
  fi

  chars="$(printf '%s' "$payload" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["pagination"]["total_length"])' \
    2>/dev/null || echo "-")"

  printf '%-30s %7s %8s %8s %8s %9s  %s\n' \
    "$name" "$seconds" "$budget" "$peak" "$pct" "$chars" "$verdict"

  if [ "$status" != "200" ]; then
    echo "    $(printf '%s' "$payload" | head -c 300)"
  fi

  capture_logs "$slug"
  if [ "$verdict" != "ok" ]; then
    FAILED_BOOKS+=("$slug|$name")
  fi
done

echo
if [ "$failures" -gt 0 ]; then
  echo "FAILED: $failures budget violation(s)"
  # Guarded: expanding an empty array under `set -u` errors on bash < 4.4, and
  # a failure before any pod resolved leaves this list empty.
  if [ "${#FAILED_BOOKS[@]}" -gt 0 ]; then
    for entry in "${FAILED_BOOKS[@]}"; do
      slug="${entry%%|*}"
      label="${entry##*|}"
      found=0
      for logfile in "${LOG_DIR}/${slug}."*.log; do
        [ -f "$logfile" ] || continue
        found=1
        echo
        echo "--- ${label}: $(basename "$logfile") (last 60 lines) ---"
        tail -60 "$logfile"
      done
      if [ "$found" = "0" ]; then
        echo
        echo "--- ${label}: no pod logs captured ---"
      fi
    done
  fi
  echo
  echo "--- pod status ---"
  kubectl get pods -n "$NAMESPACE" -l app=markitdown-server 2>&1 | head -10
  exit 1
fi
echo "All books converted within time and memory budgets."
echo "(per-book pod logs in ${LOG_DIR})"
