#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker.sh"
make_review_worker "${worker}"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id reconcile-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id reconcile-run \
  --task-id T001-reconcile \
  --goal reconcile \
  --profile delegated \
  --allowed-paths file.txt >/dev/null
complete_task_contract reconcile-run T001-reconcile reconcile

events="${repo}/.agent-collab/runs/reconcile-run/EVENTS.ndjson"
chmod 400 "${events}"
set +e
RDO_WORKER_COMMAND="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" reconcile-run T001-reconcile \
  > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
code=$?
set -e
chmod 600 "${events}"
[[ "${code}" -ne 0 ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/reconcile-run/tasks/T001-reconcile")
status = json.loads((task / "STATUS.json").read_text())
attempt = json.loads(
    (task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json").read_text()
)
assert status["state"] == "blocked", status
assert status["blocker_type"] == "needs_coordinator", status
assert attempt["state"] == "invalid_handoff", attempt
assert attempt["outcome"] == "execution_failed", attempt
assert attempt["ended_at"], attempt
assert not (task / ".dispatch-lock").exists()
PY
