#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-blocked.sh"
make_blocked_worker "${worker}" "needs_user" "dataset path needs user confirmation"

init_run_and_task smoke-run T001-blocked blocked
CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-blocked
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['state'] == 'blocked'"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['blocker_type'] == 'needs_user'"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-blocked")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
assert status["blocking_reason"] == "dataset path needs user confirmation"
assert status["state_history"][-1]["from"] == "running"
assert status["state_history"][-1]["to"] == "blocked"
assert status["state_history"][-1]["actor"] == "dispatch"
assert attempt["state"] == "completed"
assert attempt["handoff_valid"] is True
assert attempt["handoff_state"] == "blocked"
PY
