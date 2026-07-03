#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: scripts/dispatch_claude.sh <run-id> <task-id>" >&2
  exit 2
fi

RUN_ID="$1"
TASK_ID="$2"
CLAUDE_CODE_CMD="${CLAUDE_CODE_CMD:-claude}"
CLAUDE_AGENT_NAME="${CLAUDE_AGENT_NAME:-claude-worker}"
CLAUDE_SESSION_ID="${CLAUDE_SESSION_ID:-}"
DISPATCH_DRY_RUN="${DISPATCH_DRY_RUN:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RUN_DIR="${REPO_ROOT}/.agent-collab/runs/${RUN_ID}"
TASK_DIR="${RUN_DIR}/tasks/${TASK_ID}"
STATUS_PATH="${TASK_DIR}/STATUS.json"
FSM_PATH="${SKILL_ROOT}/references/state-machine.json"
LOCK_PATH="${TASK_DIR}/LOCK"

if [[ ! -d "${TASK_DIR}" ]]; then
  echo "task not found: ${TASK_DIR}" >&2
  exit 2
fi

if [[ ! -f "${STATUS_PATH}" ]]; then
  echo "STATUS.json not found: ${STATUS_PATH}" >&2
  exit 2
fi

if [[ -e "${LOCK_PATH}" ]]; then
  echo "task already locked: ${LOCK_PATH}" >&2
  exit 3
fi

ATTEMPT_SEQ="$(find "${TASK_DIR}/attempts" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
ATTEMPT_NUM="$(printf "%03d" "$((ATTEMPT_SEQ + 1))")"
ATTEMPT_ID="A${ATTEMPT_NUM}-claude-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(3))
PY
)"
ATTEMPT_DIR="${TASK_DIR}/attempts/${ATTEMPT_ID}"

python3 - "$STATUS_PATH" "$FSM_PATH" "$ATTEMPT_ID" "$CLAUDE_AGENT_NAME" "$CLAUDE_SESSION_ID" <<'PY'
import json
import sys
from datetime import datetime, timezone

status_path, fsm_path, attempt_id, agent_name, session_id = sys.argv[1:6]
status = json.load(open(status_path, encoding="utf-8"))
fsm = json.load(open(fsm_path, encoding="utf-8"))
state = status.get("state")
allowed = fsm["transitions"].get(state, {}).get("running", [])
if "dispatch" not in allowed:
    raise SystemExit(f"illegal dispatch transition: {state!r} -> 'running'")
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status["previous_state"] = state
status["state"] = "running"
status["owner"] = "claude-code"
status["updated_at"] = now
status["needs_codex"] = False
status["blocking_reason"] = ""
status["blocker_type"] = ""
status["current_attempt_id"] = attempt_id
status["assigned_worker"] = {
    "agent": "claude-code",
    "agent_name": agent_name,
    "session_id": session_id,
    "role": "worker",
}
status.setdefault("state_history", []).append({
    "from": state,
    "to": "running",
    "actor": "dispatch",
    "at": now,
})
json.dump(status, open(status_path, "w", encoding="utf-8"), indent=2)
open(status_path, "a", encoding="utf-8").write("\n")
PY

mkdir -p "${ATTEMPT_DIR}"

BRANCH="$(python3 - "$STATUS_PATH" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("branch", ""))
PY
)"
WORKTREE_REL="$(python3 - "$STATUS_PATH" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("worktree", ""))
PY
)"
WORKTREE_PATH="${REPO_ROOT}/${WORKTREE_REL}"

{
  echo "owner: dispatch"
  echo "pid: $$"
  echo "created_at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "command: $0 $RUN_ID $TASK_ID"
  echo "attempt_id: ${ATTEMPT_ID}"
} > "${LOCK_PATH}"

if [[ "${DISPATCH_DRY_RUN}" != "1" ]]; then
  if [[ ! -d "${WORKTREE_PATH}" ]]; then
    if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
      git worktree add "${WORKTREE_PATH}" "${BRANCH}"
    else
      git worktree add -b "${BRANCH}" "${WORKTREE_PATH}" HEAD
    fi
  fi
fi

python3 - "$ATTEMPT_DIR/ATTEMPT.json" "$ATTEMPT_ID" "$TASK_ID" "$CLAUDE_AGENT_NAME" "$CLAUDE_SESSION_ID" "$CLAUDE_CODE_CMD" "$WORKTREE_PATH" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, attempt_id, task_id, agent_name, session_id, command, cwd = sys.argv[1:8]
payload = {
    "attempt_id": attempt_id,
    "task_id": task_id,
    "agent": "claude-code",
    "agent_name": agent_name,
    "session_id": session_id,
    "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "ended_at": None,
    "runtime": {
        "model": os.environ.get("CLAUDE_MODEL"),
        "cli": command.split()[0] if command.split() else command,
        "command": command,
        "cwd": cwd,
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
PY

{
  echo "# Worker Task Prompt"
  echo
  echo "You are a Claude Code worker. Execute only this task packet."
  echo
  echo "Protocol reminders:"
  echo "- You may only transition STATUS.json from running to review or blocked."
  echo "- Do not write approved, merged, failed, or changes_requested."
  echo "- Write EVIDENCE.md and HANDOFF.md before ending."
  echo "- Keep changes inside allowed_paths."
  echo
  echo "## TASK.md"
  cat "${TASK_DIR}/TASK.md"
  echo
  echo "## CONTEXT.md"
  cat "${TASK_DIR}/CONTEXT.md"
  echo
  echo "## ACCEPTANCE.md"
  cat "${TASK_DIR}/ACCEPTANCE.md"
} > "${ATTEMPT_DIR}/prompt.md"

if [[ "${DISPATCH_DRY_RUN}" == "1" ]]; then
  echo "dry run: prompt written to ${ATTEMPT_DIR}/prompt.md" | tee "${ATTEMPT_DIR}/result.md"
  touch "${ATTEMPT_DIR}/transcript.log"
else
  set +e
  (cd "${WORKTREE_PATH}" && ${CLAUDE_CODE_CMD} < "${ATTEMPT_DIR}/prompt.md") \
    > "${ATTEMPT_DIR}/transcript.log" 2>&1
  EXIT_CODE=$?
  set -e
  {
    echo "# Worker Result"
    echo
    echo "exit_code: ${EXIT_CODE}"
  } > "${ATTEMPT_DIR}/result.md"
fi

python3 - "$STATUS_PATH" "$ATTEMPT_ID" "$TASK_DIR" <<'PY'
import json
import sys
from pathlib import Path

status_path, attempt_id, task_dir = sys.argv[1:4]
status = json.load(open(status_path, encoding="utf-8"))
state = status.get("state")
valid_state = state in {"review", "blocked"}
valid_attempt = status.get("current_attempt_id") == attempt_id
evidence_or_handoff = (
    Path(task_dir, "EVIDENCE.md").exists()
    and Path(task_dir, "EVIDENCE.md").read_text(encoding="utf-8").strip()
) or (
    Path(task_dir, "HANDOFF.md").exists()
    and Path(task_dir, "HANDOFF.md").read_text(encoding="utf-8").strip()
)
if not (valid_state and valid_attempt and evidence_or_handoff):
    print("worker_exit_without_valid_status", file=sys.stderr)
    print(f"state={state!r} current_attempt_id={status.get('current_attempt_id')!r}", file=sys.stderr)
    raise SystemExit(4)
PY
