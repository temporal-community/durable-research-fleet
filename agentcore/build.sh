#!/usr/bin/env bash
# Build the AgentCore Runtime code zip: agentcore/build/code.zip.
#
# Two things here are load-bearing:
#
#  1. Dependencies are resolved for the RUNTIME's platform (linux/arm64, CPython 3.12),
#     not for this machine. `pip install` on a Mac would ship darwin wheels that fail
#     at import inside the session, with an error that does not point back here.
#
#  2. The zip is DETERMINISTIC (sorted entries, fixed timestamps). Terraform keys the
#     S3 object on the zip's md5, and a new object means a new Runtime version. A zip
#     that differs on every build would publish a version per apply and, with it, a
#     version no Build ID refers to.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
OUT=agentcore/build
rm -rf "$OUT"
mkdir -p "$OUT/pkg"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

uv pip install --quiet \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  --target "$OUT/pkg" \
  -r requirements.txt -r requirements-agentcore.txt

# Only what a Worker session needs. The web tier (web.py, web/) is a Temporal CLIENT and
# runs elsewhere; shipping it would just be dead weight in every session.
cp runtime.py agentcore_runtime.py worker_agentcore.py apps.py \
   workflows.py activities.py \
   llm.py research_types.py research_activities.py research_workflow.py "$OUT/pkg/"

python3 - "$OUT/pkg" "$OUT/code.zip" <<'PY'
import os, sys, zipfile
src, dst = sys.argv[1:]
files = sorted(
    os.path.relpath(os.path.join(d, f), src)
    for d, dirs, fs in os.walk(src)
    for f in fs
    if not f.endswith(".pyc") and "__pycache__" not in d
)
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for rel in files:
        info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        mode = os.stat(os.path.join(src, rel)).st_mode & 0o777 or 0o644
        info.external_attr = (mode | 0o100000) << 16
        with open(os.path.join(src, rel), "rb") as fh:
            z.writestr(info, fh.read())
print(f"{len(files)} files")
PY

SIZE=$(du -h "$OUT/code.zip" | cut -f1)
HASH=$(python3 -c "import hashlib,sys;print(hashlib.md5(open('$OUT/code.zip','rb').read()).hexdigest())")
echo "built $OUT/code.zip ($SIZE, md5 $HASH)"
