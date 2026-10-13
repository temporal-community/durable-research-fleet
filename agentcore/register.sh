#!/usr/bin/env bash
# Register this build's endpoint with Temporal Cloud as a Worker Deployment Version.
#
#   register.sh                 create the deployment + version (idempotent)
#   SET_CURRENT=1 register.sh   ...and route new Workflows to it
#
# Temporal reads TEMPORAL_ADDRESS / TEMPORAL_NAMESPACE / TEMPORAL_API_KEY from the
# environment (.env here). The guard below is the "this Namespace only" control: the
# stack was applied for ONE Namespace, and registering its Runtime into any other
# would hand that Namespace the right to invoke it.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }
out() { terraform -chdir=agentcore output -raw "$1"; }

NS=$(out temporal_namespace)
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-$NS.tmprl.cloud:7233}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-$NS}"
: "${TEMPORAL_API_KEY:?set TEMPORAL_API_KEY in .env}"

if [ "$TEMPORAL_NAMESPACE" != "$NS" ]; then
  echo "refusing: TEMPORAL_NAMESPACE=$TEMPORAL_NAMESPACE but this stack serves $NS" >&2
  exit 1
fi

DEPLOYMENT=$(out deployment_name)
BUILD=$(out build_id)

temporal worker deployment create --name "$DEPLOYMENT" 2>&1 \
  | grep -v -i "already exists" || true

if temporal worker deployment describe-version \
     --deployment-name "$DEPLOYMENT" --build-id "$BUILD" >/dev/null 2>&1; then
  echo "version $DEPLOYMENT/$BUILD already registered"
else
  temporal worker deployment create-version \
    --deployment-name "$DEPLOYMENT" \
    --build-id "$BUILD" \
    --aws-agentcore-endpoint-arn "$(out endpoint_arn)" \
    --aws-agentcore-assume-role-arn "$(out invoker_role_arn)" \
    --aws-agentcore-assume-role-external-id "$(out external_id)"
fi

if [ "${SET_CURRENT:-0}" = 1 ]; then
  temporal worker deployment set-current-version \
    --deployment-name "$DEPLOYMENT" --build-id "$BUILD" --yes
fi

temporal worker deployment describe-version \
  --deployment-name "$DEPLOYMENT" --build-id "$BUILD"
