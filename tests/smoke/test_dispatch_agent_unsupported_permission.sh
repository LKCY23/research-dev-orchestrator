#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"

init_raw_run_and_task smoke-run T001-permission permission
set +e
"${RDO_ROOT}/scripts/dispatch_agent.sh" smoke-run T001-permission \
  --worker opencode \
  --runtime plain \
  --io machine \
  --permission yolo > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
rc=$?
set -e

if [[ "${rc}" -eq 0 ]]; then
  echo "dispatch unexpectedly succeeded" >&2
  exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-permission")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
assert status["state"] == "pending", status
assert not (task / ".dispatch-lock").exists()
assert not (task / "LOCK").exists()
assert list((task / "attempts").iterdir()) == []
PY
