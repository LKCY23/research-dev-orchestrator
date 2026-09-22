#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-sleep.sh"
sentinel="${repo}/late.txt"
release="${repo}/release-descendant"
started="${repo}/descendant.pid"
make_detached_child_worker "${worker}" "${sentinel}" "${release}" "${started}"

init_run_and_task smoke-run T001-timeout timeout
set +e
RDO_WORKER_BACKEND=tmux RDO_IO_MODE=human RDO_TMUX_WAIT_TIMEOUT_SECONDS=5 CLAUDE_CODE_CMD="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-timeout
code="$?"
set -e
touch "${release}"
[[ "${code}" == "5" ]] || {
  printf 'expected tmux timeout exit 5, got %s\n' "${code}" >&2
  exit 1
}

collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"

PYTHONPATH="${RDO_ROOT}/scripts" python3 - "${started}" "${sentinel}" <<'PY'
import json
from pathlib import Path
import sys

from supervisor import pid_alive

started = Path(sys.argv[1])
assert started.is_file(), "the timeout fixture never started its detached child"
child_pid = int(started.read_text())
assert not pid_alive(child_pid), f"detached child {child_pid} survived dispatch cleanup"
assert not Path(sys.argv[2]).exists(), "detached child wrote after dispatch cleanup"

task = Path(".agent-collab/runs/smoke-run/tasks/T001-timeout")
status = json.loads((task / "STATUS.json").read_text())
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
cleanup = json.loads((attempt_dir / "runtime/CLEANUP.json").read_text())
assert status["state"] == "blocked", status
assert status["blocker_type"] == "budget", status
assert attempt["outcome"] == "timed_out_unfinalized", attempt
assert attempt["exit_code"] == 124, attempt
assert cleanup["terminated"] is True, cleanup
assert cleanup["cleanup_verified"] is True, cleanup
assert cleanup["surviving_pids"] == [], cleanup
assert not (task / ".dispatch-lock").exists()
PY
