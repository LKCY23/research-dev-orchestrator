#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" --run-id delegated-run --project-slug smoke --objective smoke --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" --run-id delegated-run --task-id T001-delegated --goal delegated --profile delegated --allowed-paths file.txt >/dev/null
complete_task_contract delegated-run T001-delegated delegated
worker="${repo}/delegated-worker.sh"
make_review_worker "${worker}"
RDO_WORKER_COMMAND="${worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" delegated-run T001-delegated >/dev/null

task="${repo}/.agent-collab/runs/delegated-run/tasks/T001-delegated"
python3 "${RDO_ROOT}/scripts/collect_status.py" --run-id delegated-run --json > "${repo}/delegated-first.json"
assert_json_expr "${repo}/delegated-first.json" "payload['valid'] is True"
mkdir -p "${task}/reviews"
printf '# Findings\n\n- Apply the requested focused fix.\n' > "${task}/reviews/findings.md"
python3 "${RDO_ROOT}/scripts/rdo.py" task review \
  --task-dir "${task}" \
  --decision changes_requested \
  --reviewer codex \
  --findings-file "${task}/reviews/findings.md" >/dev/null
RDO_WORKER_COMMAND="${worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" delegated-run T001-delegated >/dev/null
python3 "${RDO_ROOT}/scripts/collect_status.py" --run-id delegated-run --json > "${repo}/delegated-second.json"
assert_json_expr "${repo}/delegated-second.json" "payload['valid'] is True"

python3 - "${task}" <<'PY'
import json, sys
from pathlib import Path
task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
attempts = sorted((task / "attempts").glob("*/ATTEMPT.json"))
assert len(attempts) == 2
first, second = [json.loads(path.read_text()) for path in attempts]
assert status["state"] == "review"
assert first["worker_id"] == second["worker_id"]
assert first["session_id"] == second["session_id"]
assert second["parent_attempt_id"] == first["attempt_id"]
assert second["execution_mode"] == "resume"
assert second["resume_reason"] == "changes_requested"
policy = json.loads((task / "EXECUTION_POLICY.json").read_text())
assert policy["strategy_required"] is False
pointer = json.loads((task / "reviews" / "CURRENT_TASK_REVIEW.json").read_text())
assert pointer["revision"] == 1
PY
