#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-generic generic
"${RDO_ROOT}/scripts/dispatch_agent.sh" smoke-run T001-generic \
  --worker codex \
  --runtime plain \
  --io machine \
  --permission auto \
  --agent-name generic-worker \
  --command "${worker}"

collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-generic")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
assert status["assigned_worker"]["backend_id"] == "codex", status
assert attempt["backend_id"] == "codex", attempt
assert attempt["runtime"]["runtime_backend"] == "plain", attempt
assert attempt["runtime"]["io_mode"] == "machine", attempt
PY
