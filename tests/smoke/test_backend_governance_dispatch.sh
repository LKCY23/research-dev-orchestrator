#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_run_and_task governance-run T001-governance governance
task="${repo}/.agent-collab/runs/governance-run/tasks/T001-governance"

set +e
RDO_WORKER_BACKEND=codex RDO_WORKER_COMMAND=/usr/bin/true \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" governance-run T001-governance \
  >"${repo}/dispatch.out" 2>"${repo}/dispatch.err"
code=$?
set -e

test "${code}" -eq 2
grep -q "does not match dispatch backend" "${repo}/dispatch.err"
test ! -d "${task}/.dispatch-lock"
test "$(find "${task}/attempts" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" -eq 0
python3 - "${task}/STATUS.json" <<'PY'
import json, sys
status = json.load(open(sys.argv[1], encoding="utf-8"))
assert status["state"] == "strategy_review", status
assert status["current_attempt_id"] is None, status
PY
