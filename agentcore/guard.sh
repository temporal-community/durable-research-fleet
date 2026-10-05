#!/usr/bin/env bash
# Refuse an apply that changes the code but not the Build ID.
#
# That combination applies cleanly and does nothing visible: Terraform publishes a new
# Runtime version, but the endpoint for the unchanged Build ID is held on its old version
# (`ignore_changes`, which is what keeps pinned Workflows on the code they started on).
# Temporal keeps invoking the old code and nothing reports it. This is the AgentCore form
# of AGENTS.md gate #14.
#
# Override with ALLOW_UNPINNED=1 when the change is deliberate (e.g. infra-only).
set -Eeuo pipefail
cd "$(dirname "$0")/.."

applied_md5=$(terraform -chdir=agentcore output -raw code_md5 2>/dev/null || true)
applied_build=$(terraform -chdir=agentcore output -raw build_id 2>/dev/null || true)
[ -z "$applied_md5" ] && exit 0   # nothing applied yet

new_md5=$(python3 -c "import hashlib;print(hashlib.md5(open('agentcore/build/code.zip','rb').read()).hexdigest())")
want_build=$(python3 - <<'PY'
import os,re
if os.environ.get("TF_VAR_build_id"):
    print(os.environ["TF_VAR_build_id"]); raise SystemExit
m=re.search(r'^\s*build_id\s*=\s*"([^"]+)"', open("agentcore/terraform.tfvars").read(), re.M)
print(m.group(1) if m else "")
PY
)

if [ "$new_md5" != "$applied_md5" ] && [ "$want_build" = "$applied_build" ] && [ "${ALLOW_UNPINNED:-0}" != 1 ]; then
  echo "refusing: the code changed but build_id is still $applied_build." >&2
  echo "  A new Runtime version would be published that NO endpoint references, so Temporal" >&2
  echo "  would keep invoking the old code. Bump build_id in agentcore/terraform.tfvars and add" >&2
  echo "  $applied_build to retained_build_ids. (ALLOW_UNPINNED=1 overrides.)" >&2
  exit 1
fi

# Published zips are APPEND-ONLY. A Runtime version built from the previous zip (pinned to
# a Build ID that is still running or parked in review) may re-read it, and Terraform
# would otherwise delete the old object when the new one replaces it. Forget the old
# object (state only; nothing is deleted in AWS) so it stays in the bucket, and let the
# new zip be created under its own content-hash key.
if [ "$new_md5" != "$applied_md5" ]; then
  if terraform -chdir=agentcore state list 2>/dev/null | grep -qx 'aws_s3_object.code'; then
    cp agentcore/terraform.tfstate "agentcore/terraform.tfstate.pre-${want_build:-build}.bak" 2>/dev/null || true
    terraform -chdir=agentcore state rm aws_s3_object.code >/dev/null
    echo "kept the previous zip: removed aws_s3_object.code from state (object stays in S3)"
  fi
fi
