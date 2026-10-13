#!/usr/bin/env bash
# Live end-to-end check of the AgentCore deployment, spending no model tokens: it runs
# the HelloWorkflow smoke test, exactly as `make verify SCALE=1` does for Cloud Run.
#
# What it proves, in order:
#   1. the Namespace is the one this stack serves
#   2. the Worker Deployment Version exists and its compute provider is AgentCore
#   3. it is the CURRENT version (otherwise Workflows never dispatch)
#   4. Temporal Cloud really INVOKES the Runtime: N Workflows complete with no Worker
#      running beforehand
#   5. the fan-out is real: more than one distinct Worker identity polled
#   6. the Workers drain: pollers disappear after the idle window
#
# COUNT (default 5) Workflows, one 5s Activity each, one slot per session.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
out() { terraform -chdir=agentcore output -raw "$1"; }

NS=$(out temporal_namespace); DEP=$(out deployment_name); BUILD=$(out build_id)
export TEMPORAL_ADDRESS="$NS.tmprl.cloud:7233" TEMPORAL_NAMESPACE="$NS"
TQ="${TEMPORAL_TASK_QUEUE:-research-queue}"; COUNT="${COUNT:-5}"
pass=0; fail=0; p=0; peak=0; WORKERS=()
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }

pollers() {  # distinct poller identities, wherever the describe output nests them
  # On a VERSIONED Task Queue the top-level `pollers` is null (measured live 2026-10-05),
  # so walk the whole document rather than trusting one field.
  temporal task-queue describe --task-queue "$TQ" --task-queue-type workflow --select-all-active -o json 2>/dev/null \
    | python3 -c "
import json,sys
ids=set()
def walk(o):
    if isinstance(o,dict):
        if o.get('identity'): ids.add(o['identity'])
        for v in o.values(): walk(v)
    elif isinstance(o,list):
        for v in o: walk(v)
walk(json.load(sys.stdin)); print(len(ids))" 2>/dev/null || echo 0
}

# The authoritative fan-out measure: distinct Worker identities that actually STARTED an
# Activity, read from each Workflow's history. Poller listings are a sampled view; the
# history is a record.
workers_from_history() {
  for id in "$@"; do
    temporal workflow show --workflow-id "$id" --detailed 2>/dev/null \
      | awk '/ActivityTaskStarted/{f=1} f&&/identity:/{print $2; f=0}'
  done | sort -u
}

echo "== 1-3. Namespace and Version"
[ "$TEMPORAL_NAMESPACE" = "$NS" ] && ok "namespace $NS" || bad "namespace"
VER=$(temporal worker deployment describe-version --deployment-name "$DEP" --build-id "$BUILD" 2>&1 || true)
echo "$VER" | grep -q "aws-agentcore" && ok "compute provider is aws-agentcore" || bad "compute provider (is the version registered?)"
CUR=$(temporal worker deployment describe --name "$DEP" 2>&1 || true)
echo "$CUR" | grep -E "CurrentVersionBuildID" | grep -q "$BUILD" && ok "$BUILD is the current version" || bad "$BUILD is not current (SET_CURRENT=1 make ac-register)"

echo "== 4. Temporal Cloud invokes the Runtime ($COUNT Workflows, no Worker running)"
start=$(date +%s); ids=()
for i in $(seq 1 "$COUNT"); do
  id="ac-verify-$(date +%s)-$i"; ids+=("$id")
  temporal workflow start --task-queue "$TQ" --type HelloWorkflow --workflow-id "$id" \
    --input '"agentcore"' >/dev/null
done
peak=0; done_n=0
while [ $(( $(date +%s) - start )) -lt "${TIMEOUT:-240}" ]; do
  p=$(pollers); [ "$p" -gt "$peak" ] && peak=$p
  done_n=0
  for id in "${ids[@]}"; do
    s=$(temporal workflow describe --workflow-id "$id" -o json 2>/dev/null \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['workflowExecutionInfo']['status'])" 2>/dev/null || echo "")
    [ "$s" = "WORKFLOW_EXECUTION_STATUS_COMPLETED" ] && done_n=$((done_n+1))
  done
  echo "  t+$(( $(date +%s) - start ))s  completed=$done_n/$COUNT  pollers=$p"
  [ "$done_n" -eq "$COUNT" ] && break
  sleep 5
done
[ "$done_n" -eq "$COUNT" ] && ok "all $COUNT Workflows completed in $(( $(date +%s) - start ))s" || bad "only $done_n/$COUNT completed"

echo "== 5. Fan-out (distinct Workers that ran an Activity, from history)"
# macOS ships bash 3.2, which has no mapfile; word-splitting is safe for identities.
WORKERS=($(workers_from_history "${ids[@]}"))
n=${#WORKERS[@]}; for w in ${WORKERS[@]+"${WORKERS[@]}"}; do echo "  worker: $w"; done
[ "$n" -gt 1 ] && ok "$n distinct Workers served $COUNT Workflows" || bad "only $n Worker served $COUNT Workflows (fan-out not happening; check slots and identities)"
echo "  (informational) peak pollers seen: $peak -- scaling is the scaler's call, not one session per task"

echo "== 6. Scale-in (idle window is AGENTCORE_DEBOUNCE_SECONDS)"
if [ "$peak" -eq 0 ]; then
  # The poller view never showed a Worker, so "0 pollers" proves nothing: it would read 0
  # whether or not anything drained. Say so rather than PASS on a vacuous check. The
  # session log line "drained and releasing the session" is the real evidence.
  echo "  SKIP  poller view was empty throughout; confirm drain in the session logs:"
  echo "        aws logs filter-log-events --region <region> --log-group-name <runtime log group> --filter-pattern drained"
else
  end=$(( $(date +%s) + ${DRAIN_WAIT:-300} ))
  while [ "$(date +%s)" -lt "$end" ]; do
    p=$(pollers); echo "  pollers=$p"; [ "$p" -eq 0 ] && break; sleep 10
  done
  [ "$p" -eq 0 ] && ok "Workers drained to 0" || bad "still $p pollers after the wait (entries age out; re-check)"
fi

echo; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
