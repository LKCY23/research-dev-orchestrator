#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
unset RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE

repo="$(setup_smoke_repo)"
cd "${repo}"
init_raw_run_and_task smoke-run T001-override override

set +e
RDO_WORKER_COMMAND=/usr/bin/true \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-override \
  > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
rc=$?
set -e
[[ "${rc}" -eq 2 ]]
grep -q "do not provide a registered startup-event contract" "${repo}/dispatch.err"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-override")
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
assert status["state"] == "pending", status
assert not (task / ".dispatch-lock").exists()
assert not (task / "LOCK").exists()
assert not list((task / "attempts").iterdir())
PY
