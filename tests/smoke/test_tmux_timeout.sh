#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-sleep.sh"
make_sleep_worker "${worker}" 2

init_run_and_task smoke-run T001-timeout timeout
set +e
RDO_WORKER_BACKEND=tmux RDO_IO_MODE=human RDO_TMUX_WAIT_TIMEOUT_SECONDS=1 CLAUDE_CODE_CMD="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-timeout
code="$?"
set -e
[[ "${code}" == "5" ]]
sleep 3

set +e
collect_json smoke-run "${repo}/status.json"
set -e
assert_json_expr "${repo}/status.json" "payload['valid'] is False"

python3 - <<'PY'
import json
import subprocess
from pathlib import Path

payload = json.load(open("status.json", encoding="utf-8"))
violations = "\n".join(payload["protocol_violations"])
assert ".dispatch-lock pid is not alive while STATUS is running" in violations
assert "tmux exit_code file exists while STATUS and ATTEMPT still report running" in violations
task = Path(".agent-collab/runs/smoke-run/tasks/T001-timeout")
assert (task / ".dispatch-lock").is_dir()
session = (task / ".dispatch-lock" / "tmux_session").read_text(encoding="utf-8").strip()
subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
PY
