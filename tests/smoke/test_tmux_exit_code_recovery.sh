#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_run_and_task smoke-run T001-grace grace

sleep 120 &
alive_pid="$!"
trap 'code=$?; kill "${alive_pid}" 2>/dev/null || true; wait "${alive_pid}" 2>/dev/null || true; cleanup_smoke_repos; exit "${code}"' EXIT

python3 - "${alive_pid}" <<'PY'
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

pid = sys.argv[1]
task = pathlib.Path(".agent-collab/runs/smoke-run/tasks/T001-grace")
attempt_id = "A001-claude-test"
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
status.update({
    "previous_state": "pending",
    "state": "running",
    "owner": "claude-code",
    "updated_at": now,
    "current_attempt_id": attempt_id,
    "assigned_worker": {"agent": "claude-code", "agent_name": "test", "session_id": "", "role": "worker"},
})
status["state_history"] = [{"from": "pending", "to": "running", "actor": "dispatch", "at": now}]
(task / "STATUS.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
attempt_dir = task / "attempts" / attempt_id
attempt_dir.mkdir(parents=True)
attempt = {
    "attempt_id": attempt_id,
    "task_id": "T001-grace",
    "agent": "claude-code",
    "agent_name": "test",
    "session_id": "",
    "state": "running",
    "handoff_valid": None,
    "handoff_state": None,
    "started_at": now,
    "ended_at": None,
    "exit_code": None,
    "runtime": {
        "backend": "tmux",
        "model": None,
        "cli": "fake",
        "command": "fake",
        "cwd": str(pathlib.Path.cwd()),
        "tmux_session": "rdo-test",
        "attach_command": "tmux attach -t rdo-test",
    },
}
(attempt_dir / "ATTEMPT.json").write_text(json.dumps(attempt, indent=2) + "\n", encoding="utf-8")
(attempt_dir / "exit_code").write_text("0\n", encoding="utf-8")
(task / "LOCK").write_text(f"attempt_id: {attempt_id}\n", encoding="utf-8")
lock = task / ".dispatch-lock"
lock.mkdir()
(lock / "attempt_id").write_text(attempt_id + "\n", encoding="utf-8")
(lock / "pid").write_text(pid + "\n", encoding="utf-8")
PY

collect_json smoke-run "${repo}/grace.json"
assert_json_expr "${repo}/grace.json" "payload['valid'] is True"
assert_json_expr "${repo}/grace.json" "'handoff validation may be in progress' in '\\n'.join(payload['protocol_warnings'])"

python3 - <<'PY'
import os
import pathlib
import time

path = pathlib.Path(".agent-collab/runs/smoke-run/tasks/T001-grace/attempts/A001-claude-test/exit_code")
old = time.time() - 120
os.utime(path, (old, old))
PY

set +e
collect_json smoke-run "${repo}/stale.json"
set -e
assert_json_expr "${repo}/stale.json" "payload['valid'] is False"
assert_json_expr "${repo}/stale.json" "'tmux exit_code file exists while STATUS and ATTEMPT still report running' in '\\n'.join(payload['protocol_violations'])"
