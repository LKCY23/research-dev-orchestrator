#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
fake_bin="${repo}/fake-bin"
fake_home="${repo}/fake-claude-home"
session_id="33333333-3333-3333-3333-333333333333"
mkdir -p "${fake_bin}" "${fake_home}/projects/project"
printf '{}\n' > "${fake_home}/projects/project/${session_id}.jsonl"

cat > "${fake_bin}/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  echo "fake-claude 2.1.185"
  exit 0
fi
if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
  echo '{"loggedIn":true}'
  exit 0
fi
if [[ " $* " == *" --help "* ]]; then
  echo "fake Claude help"
  exit 0
fi
if [[ "$*" == *" --resume "* ]]; then
  echo "No conversation found with session ID: 33333333-3333-3333-3333-333333333333"
  exit 1
fi
[[ " $* " == *" --session-id 33333333-3333-3333-3333-333333333333 "* ]]
prompt="${!#}"
attempt_dir="$(printf '%s\n' "${prompt}" | awk -F': ' '/^- ATTEMPT_DIR:/ {print $2}')"
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "${attempt_dir}" \
  --check-id smoke >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "${attempt_dir}" \
  --state review \
  --summary "human resume fallback completed" >/dev/null
SH
freeze_worker_rdo_root "${fake_bin}/claude"
chmod +x "${fake_bin}/claude"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id human-fallback-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id human-fallback-run \
  --task-id T001-human-fallback \
  --goal fallback \
  --profile delegated \
  --allowed-paths file.txt >/dev/null
complete_task_contract human-fallback-run T001-human-fallback fallback

task="${repo}/.agent-collab/runs/human-fallback-run/tasks/T001-human-fallback"
python3 - "${task}" "${session_id}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
session_id = sys.argv[2]
status_path = task / "STATUS.json"
status = json.loads(status_path.read_text())
status.update(
    state="blocked",
    previous_state="running",
    owner="worker",
    current_attempt_id="A000-claude-prior",
    needs_coordinator=True,
    blocker_type="needs_coordinator",
    blocking_reason="retry fixture",
    assigned_worker={
        "worker_id": "W-stable-human",
        "backend_id": "claude-code",
        "agent": "claude-code",
        "agent_name": "claude-worker",
        "backend_session_id": session_id,
        "session_id": session_id,
        "first_attempt_id": "A000-claude-prior",
        "latest_attempt_id": "A000-claude-prior",
        "role": "worker",
    },
)
status["state_history"] = [
    {"from": "pending", "to": "running", "actor": "dispatch", "at": "2026-07-16T00:00:00Z"},
    {"from": "running", "to": "blocked", "actor": "dispatch", "at": "2026-07-16T00:01:00Z"},
]
status_path.write_text(json.dumps(status, indent=2) + "\n")
PY

PATH="${fake_bin}:${PATH}" \
CLAUDE_CONFIG_DIR="${fake_home}" \
RDO_RUNTIME_BACKEND=tmux \
RDO_IO_MODE=human \
RDO_TMUX_WAIT_TIMEOUT_SECONDS=15 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" \
  human-fallback-run T001-human-fallback >/dev/null

python3 - "${task}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
assert status["state"] == "review", status
assert attempt["worker_id"] == "W-stable-human", attempt
assert attempt["requested_execution_mode"] == "resume", attempt
assert attempt["execution_mode"] == "start", attempt
assert attempt["resume_fallback_reason"] == "runtime_session_not_found", attempt
assert attempt["resume_fallback"]["source"] == "runtime", attempt
assert attempt["outcome"] == "completed", attempt
assert (attempt_dir / "runtime" / "RESUME_STARTUP_FAILURE.json").exists()
PY
