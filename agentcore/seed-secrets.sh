#!/usr/bin/env bash
# Seed the three Secrets Manager secrets from .env. Values never touch Terraform state,
# the command line (visible in `ps`), or this script's output.
#
# Never overwrites a secret that already has a value unless FORCE=1.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }
REGION=$(terraform -chdir=agentcore output -raw region 2>/dev/null || echo "${AWS_REGION:-ca-central-1}")

tmp=$(mktemp); chmod 600 "$tmp"; trap 'rm -f "$tmp"' EXIT

for name in TEMPORAL_API_KEY GEMINI_API_KEY ANTHROPIC_API_KEY; do
  arn=$(terraform -chdir=agentcore output -json secret_arns | python3 -c "import json,sys;print(json.load(sys.stdin)['$name'])")
  value="${!name:-}"
  if [ -z "$value" ]; then
    echo "skip   $name  (not set in .env)"; continue
  fi
  if [ "${FORCE:-0}" != 1 ] && aws secretsmanager get-secret-value --secret-id "$arn" \
       --region "$REGION" --query VersionId --output text >/dev/null 2>&1; then
    echo "keep   $name  (already has a value; FORCE=1 to replace)"; continue
  fi
  printf '%s' "$value" > "$tmp"
  aws secretsmanager put-secret-value --secret-id "$arn" --region "$REGION" \
    --secret-string "file://$tmp" --query VersionId --output text >/dev/null
  echo "seeded $name"
done
