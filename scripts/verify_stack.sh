#!/usr/bin/env bash
# Verifies a LIVE deployment. `terraform test` proves the config is right and
# pytest proves the app is right; neither can prove the deployed stack actually
# scales. These are the checks that only mean something against real GCP.
#
#   make verify          # checks only
#   make verify SCALE=1  # also run the full scale-from-zero cycle (~4 min)
#
# Exit non-zero on the first hard failure so CI can gate on it.
set -uo pipefail

PROJECT="${PROJECT:-serverless-workers-demo}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
PREFIX="${PREFIX:-research-fleet}"
BUILD_ID="${BUILD_ID:-v1}"
TASK_QUEUE="${TASK_QUEUE:-research-queue}"
SCALE="${SCALE:-0}"

VM="$PREFIX-temporal"
POOL="$PREFIX-worker-pool"
INVOKER="$PREFIX-invoker@$PROJECT.iam.gserviceaccount.com"
RUNTIME_SA="$PREFIX-worker-rt@$PROJECT.iam.gserviceaccount.com"

pass=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }

on_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --quiet --command "$1" 2>/dev/null; }
T="/opt/temporal/temporal --address 127.0.0.1:7233 --namespace default"

echo "=== 1. Temporal service ==="
[ "$(on_vm 'systemctl is-active temporal')" = "active" ] \
  && ok "temporal.service active" || bad "temporal.service not active"
on_vm "$T operator cluster health" | grep -q SERVING \
  && ok "frontend SERVING" || bad "frontend not SERVING"

echo "=== 2. VM bootstrap completed ==="
if on_vm 'test -f /var/log/bb-ready' ; then
  ok "bootstrap marker /var/log/bb-ready present"
elif on_vm 'test -f /var/log/bb-failed'; then
  bad "bootstrap FAILED at: $(on_vm 'cat /var/log/bb-failed')"
else
  bad "bootstrap never finished (no marker)"
fi

echo "=== 3. Worker Controller enabled with the right algorithms ==="
flags=$(on_vm 'cat /opt/temporal/start.sh')
grep -q 'workercontroller.enabled=true' <<<"$flags" \
  && ok "workercontroller.enabled" || bad "workercontroller.enabled missing"
grep -q 'gcp-cloud-run' <<<"$flags" \
  && ok "gcp-cloud-run compute provider enabled" || bad "gcp-cloud-run not enabled"
# Cloud Run needs BOTH; the published docs list only no-sync (the Lambda setup).
grep -q 'no-sync' <<<"$flags" && grep -q 'rate-based' <<<"$flags" \
  && ok "no-sync AND rate-based enabled" || bad "scaling algorithms incomplete (need no-sync + rate-based)"

echo "=== 4. Worker Deployment Version ==="
ver=$(on_vm "$T worker deployment describe-version --deployment-name $PREFIX --build-id $BUILD_ID")
grep -q 'gcp-cloud-run' <<<"$ver" \
  && ok "version carries the Cloud Run compute config" || bad "version has NO compute config"
# Without Task Queues the WCI has nothing to watch and an empty pool stays empty.
grep -q "$PREFIX-queue\|research-queue" <<<"$ver" \
  && ok "Task Queues attached to the version" \
  || bad "no Task Queues on the version — run 'make register-queues'"
grep -q 'CurrentSinceTime' <<<"$ver" \
  && ok "version is current" || bad "version is not current"

echo "=== 5. IAM: the bindings whose absence fails silently ==="
# THE one. Without actAs on the RUNTIME SA, UpdateWorkerSetSize fails every
# attempt and the pool never scales, with no error visible anywhere in GCP.
gcloud iam service-accounts get-iam-policy "$RUNTIME_SA" --project "$PROJECT" --format=json 2>/dev/null \
  | grep -q 'roles/iam.serviceAccountUser' \
  && ok "invoker has actAs on the runtime SA" \
  || bad "MISSING actAs on runtime SA — the pool will never scale (silently)"

gcloud iam service-accounts get-iam-policy "$INVOKER" --project "$PROJECT" --format=json 2>/dev/null \
  | grep -q 'roles/iam.serviceAccountTokenCreator' \
  && ok "VM can impersonate the invoker" || bad "VM cannot impersonate the invoker"

gcloud projects get-iam-policy "$PROJECT" --flatten='bindings[].members' \
  --filter="bindings.members:$INVOKER" --format='value(bindings.role)' 2>/dev/null \
  | grep -q 'roles/run.developer' \
  && ok "invoker has run.developer" || bad "invoker missing run.developer"

gcloud services list --enabled --project "$PROJECT" 2>/dev/null | grep -q iamcredentials \
  && ok "iamcredentials API enabled" || bad "iamcredentials API disabled (getAccessToken will fail)"

echo "=== 6. Network: the frontend must not be public ==="
fw=$(gcloud compute firewall-rules describe "$PREFIX-allow-temporal-internal" --project "$PROJECT" \
       --format='value(sourceRanges.list())' 2>/dev/null)
[ -n "$fw" ] && ! grep -q '0.0.0.0/0' <<<"$fw" \
  && ok "7233 restricted to $fw" || bad "7233 exposed too widely: ${fw:-<rule missing>}"

echo "=== 7. Pool shape ==="
pool=$(gcloud run worker-pools describe "$POOL" --region "$REGION" --project "$PROJECT" 2>/dev/null)
grep -qi 'Scaling: *Manual' <<<"$pool" \
  && ok "manual scaling (WCI-driven)" || bad "pool is not in manual scaling mode"

echo
if [ "$SCALE" = "1" ]; then
  echo "=== 8. Scale-from-zero (the real test) ==="
  gcloud run worker-pools update "$POOL" --instances 0 --region "$REGION" --project "$PROJECT" >/dev/null 2>&1
  sleep 15
  echo "  pool forced to 0; queueing 5 Workflows..."
  on_vm "for i in 1 2 3 4 5; do $T workflow start --type HelloWorkflow \
     --task-queue $TASK_QUEUE --workflow-id verify-\$i-\$(date +%s) --input '\"verify\"' >/dev/null; done" >/dev/null

  scaled=0
  for i in $(seq 1 12); do
    sleep 20
    n=$(gcloud run worker-pools describe "$POOL" --region "$REGION" --project "$PROJECT" 2>/dev/null \
        | grep -oE 'Instances: [0-9]+' | grep -oE '[0-9]+')
    echo "    t+$((i*20))s instances=$n"
    if [ "${n:-0}" -gt 0 ]; then scaled=1; break; fi
  done
  [ "$scaled" = "1" ] \
    && ok "WCI scaled the pool from zero" \
    || bad "pool never scaled — check: journalctl -u temporal | grep UpdateWorkerSetSize"
  echo
fi

echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ] || exit 1
