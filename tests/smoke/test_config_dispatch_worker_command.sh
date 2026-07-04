#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/custom worker.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-config-worker worker
cat > .agent-collab/rdo.toml <<TOML
[worker]
command = "'${worker}'"
agent_name = "configured-worker"

[runtime]
backend = "plain"
TOML

"${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-config-worker
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-config-worker")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
assert attempt["agent_name"] == "configured-worker", attempt
assert attempt["runtime"]["cli"].endswith("custom worker.sh"), attempt
assert "custom worker.sh" in attempt["runtime"]["command"], attempt
PY
