#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-plain plain
CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-plain
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['state'] == 'review'"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['handoff_index']['requested_state'] == 'review'"
python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-plain")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = task / "attempts" / status["current_attempt_id"]
assert status["state_history"][-1]["from"] == "running"
assert status["state_history"][-1]["to"] == "review"
assert status["state_history"][-1]["actor"] == "dispatch"
assert json.load(open(attempt / "runtime" / "HANDOFF_READY.json", encoding="utf-8"))["requested_state"] == "review"
assert (attempt / "HANDOFF.json").is_file()
assert (attempt / "EVIDENCE.json").is_file()
assert not (task / "HANDOFF.json").exists()
PY
