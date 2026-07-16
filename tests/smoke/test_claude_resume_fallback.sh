#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
fake_bin="${repo}/fake-bin"
fake_home="${repo}/fake-claude-home"
mkdir -p "${fake_bin}" "${fake_home}/projects"

session_id="11111111-1111-1111-1111-111111111111"
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
printf '%s\n' "$*" > "${FAKE_CLAUDE_ARGV}"
[[ " $* " != *" --resume "* ]]
[[ " $* " == *" --session-id ${EXPECTED_SESSION_ID} "* ]]
prompt="${!#}"
printf '%s' "${prompt}" > "${FAKE_CLAUDE_PROMPT}"
attempt_dir="$(printf '%s\n' "${prompt}" | awk -F': ' '/^- ATTEMPT_DIR:/ {print $2}')"
printf '{"type":"system","subtype":"init","session_id":"%s"}\n' "${EXPECTED_SESSION_ID}"
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "${attempt_dir}" \
  --check-id smoke >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "${attempt_dir}" \
  --state review \
  --summary "resume fallback completed" >/dev/null
SH
chmod +x "${fake_bin}/claude"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id fallback-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id fallback-run \
  --task-id T001-fallback \
  --goal fallback \
  --profile delegated \
  --allowed-paths file.txt >/dev/null
complete_task_contract fallback-run T001-fallback fallback

task="${repo}/.agent-collab/runs/fallback-run/tasks/T001-fallback"
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
        "worker_id": "W-stable",
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
prior = task / "attempts" / "A000-claude-prior"
prior.mkdir(parents=True)
(prior / "ATTEMPT.json").write_text(json.dumps({
    "attempt_id": "A000-claude-prior",
    "task_id": "T001-fallback",
    "state": "invalid_handoff",
    "outcome": "startup_failed",
    "handoff_valid": False,
    "handoff_state": None,
    "phase": "execution",
    "worker_id": "W-stable",
    "backend_id": "claude-code",
    "session_id": session_id,
    "started_at": "2026-07-16T00:00:00Z",
    "ended_at": "2026-07-16T00:01:00Z",
    "exit_code": 125,
}) + "\n")
PY

PATH="${fake_bin}:${PATH}" \
CLAUDE_CONFIG_DIR="${fake_home}" \
EXPECTED_SESSION_ID="${session_id}" \
FAKE_CLAUDE_ARGV="${repo}/claude-argv.txt" \
FAKE_CLAUDE_PROMPT="${repo}/claude-prompt.txt" \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" fallback-run T001-fallback >/dev/null

python3 - "${task}" "${session_id}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
session_id = sys.argv[2]
status = json.loads((task / "STATUS.json").read_text())
attempt = json.loads(
    (task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json").read_text()
)
preflight = json.loads(
    (task / "attempts" / status["current_attempt_id"] / "runtime" / "PREFLIGHT.json").read_text()
)
assert status["state"] == "review", status
assert attempt["worker_id"] == "W-stable", attempt
assert attempt["requested_execution_mode"] == "resume", attempt
assert attempt["execution_mode"] == "start", attempt
assert attempt["requested_session_id"] == session_id, attempt
assert attempt["session_id"] == session_id, attempt
assert attempt["resume_fallback_reason"] == "session_missing", attempt
assert attempt["outcome"] == "completed", attempt
assert preflight["resume"]["session_state"] == "missing", preflight
assert preflight["resume"]["fallback_required"] is True, preflight
PY

grep -q "## TASK.md" "${repo}/claude-prompt.txt"
