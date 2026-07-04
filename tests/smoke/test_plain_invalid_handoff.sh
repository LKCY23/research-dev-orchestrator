#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review-exit1.sh"
make_review_exit1_worker "${worker}"

init_run_and_task smoke-run T001-bad bad
set +e
CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-bad
code="$?"
set -e
[[ "${code}" == "4" ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-bad")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
assert status["state"] == "review"
assert attempt["state"] == "invalid_handoff"
assert attempt["exit_code"] == 1
assert not (task / ".dispatch-lock").exists()
PY
