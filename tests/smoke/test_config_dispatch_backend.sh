#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-config-backend backend
cat > .agent-collab/rdo.toml <<TOML
[worker]
command = "${worker}"
agent_name = "config-worker"

[runtime]
backend = "tmux"
io_mode = "human"
TOML

"${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-config-backend
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-config-backend")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
assert attempt["runtime"]["backend"] == "tmux", attempt
assert attempt["agent_name"] == "config-worker", attempt
PY
