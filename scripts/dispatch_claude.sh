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
RDO_WORKER_BACKEND="${RDO_WORKER_BACKEND:-plain}"
RDO_TMUX_SESSION_PREFIX="${RDO_TMUX_SESSION_PREFIX:-rdo}"
RDO_TMUX_KEEP_SESSION="${RDO_TMUX_KEEP_SESSION:-0}"
RDO_TMUX_WAIT_TIMEOUT_SECONDS="${RDO_TMUX_WAIT_TIMEOUT_SECONDS:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RUN_DIR="${REPO_ROOT}/.agent-collab/runs/${RUN_ID}"
TASK_DIR="${RUN_DIR}/tasks/${TASK_ID}"
STATUS_PATH="${TASK_DIR}/STATUS.json"
FSM_PATH="${SKILL_ROOT}/references/state-machine.json"
LOCK_PATH="${TASK_DIR}/LOCK"
DISPATCH_LOCK_DIR="${TASK_DIR}/.dispatch-lock"
DIAGNOSTICS_DIR="${RUN_DIR}/diagnostics"
STATUS_UPDATED=0
DISPATCH_LOCK_ACQUIRED=0
KEEP_DISPATCH_LOCK_ON_EXIT=0

sanitize_name() {
  LC_ALL=C tr -c 'A-Za-z0-9_.:-' '-' | sed 's/^-*//; s/-*$//'
}

write_tmux_timeout_diagnostics() {
  mkdir -p "${DIAGNOSTICS_DIR}"
  local stamp
  stamp="$(date -u +"%Y%m%dT%H%M%SZ")"
  local json_path="${DIAGNOSTICS_DIR}/tmux-wait-timeout-${TASK_ID}-${stamp}.json"
  local md_path="${DIAGNOSTICS_DIR}/tmux-wait-timeout-${TASK_ID}-${stamp}.md"
  python3 - "$json_path" "$RUN_ID" "$TASK_ID" "${ATTEMPT_ID:-}" "${TMUX_SESSION:-}" "${TMUX_ATTACH_COMMAND:-}" "${RDO_TMUX_WAIT_TIMEOUT_SECONDS}" "${DISPATCH_LOCK_DIR}" "${ATTEMPT_DIR:-}" <<'PY'
import json
import sys
from datetime import datetime, timezone

(
    path,
    run_id,
    task_id,
    attempt_id,
    tmux_session,
    attach_command,
    timeout_seconds,
    dispatch_lock_dir,
    attempt_dir,
) = sys.argv[1:10]
payload = {
    "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "reason": "tmux_wait_timeout",
    "run_id": run_id,
    "task_id": task_id,
    "attempt_id": attempt_id,
    "tmux_session": tmux_session,
    "attach_command": attach_command,
    "timeout_seconds": int(timeout_seconds),
    "dispatch_exit_code": 5,
    "worker_exit_code": None,
    "dispatch_lock_retained": True,
    "dispatch_lock_dir": dispatch_lock_dir,
    "attempt_dir": attempt_dir,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  {
    echo "# Tmux Wait Timeout"
    echo
    echo "- run_id: ${RUN_ID}"
    echo "- task_id: ${TASK_ID}"
    echo "- attempt_id: ${ATTEMPT_ID:-}"
    echo "- reason: tmux_wait_timeout"
    echo "- dispatch_exit_code: 5"
    echo "- worker_exit_code: null"
    echo "- dispatch_lock_retained: true"
    echo "- tmux_session: ${TMUX_SESSION:-}"
    echo "- attach_command: ${TMUX_ATTACH_COMMAND:-}"
    echo "- timeout_seconds: ${RDO_TMUX_WAIT_TIMEOUT_SECONDS}"
    echo "- time: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    echo
    echo "Dispatch lost supervision before the attempt-local exit_code file appeared."
    echo "Do not assume the worker stopped. Use Lock Recovery Review before removing .dispatch-lock."
  } > "${md_path}"
}

append_event() {
  local event_name="$1"
  python3 - "$RUN_DIR" "$RUN_ID" "$TASK_ID" "$ATTEMPT_ID" "$event_name" "$CLAUDE_AGENT_NAME" "$STATUS_PATH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

run_dir, run_id, task_id, attempt_id, event_name, agent_name, status_path = sys.argv[1:8]
payload = {
    "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "actor": "dispatch" if event_name in {"task_dispatched", "worker_exit_without_valid_status"} else "claude-code",
    "event": event_name,
    "run_id": run_id,
    "task_id": task_id,
    "attempt_id": attempt_id,
}
if event_name == "task_dispatched":
    payload["worker"] = agent_name
if event_name == "worker_blocked":
    status = json.load(open(status_path, encoding="utf-8"))
    payload["blocker_type"] = status.get("blocker_type", "")
    payload["blocking_reason"] = status.get("blocking_reason", "")
with Path(run_dir, "EVENTS.ndjson").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
PY
}

dispatch_lock_matches_current_attempt() {
  [[ "${DISPATCH_LOCK_ACQUIRED}" == "1" ]] || return 1
  [[ -d "${DISPATCH_LOCK_DIR}" ]] || return 1
  [[ -f "${DISPATCH_LOCK_DIR}/attempt_id" ]] || return 1
  [[ -f "${DISPATCH_LOCK_DIR}/pid" ]] || return 1
  [[ "$(cat "${DISPATCH_LOCK_DIR}/attempt_id" 2>/dev/null)" == "${ATTEMPT_ID:-}" ]] || return 1
  [[ "$(cat "${DISPATCH_LOCK_DIR}/pid" 2>/dev/null)" == "$$" ]] || return 1
}

release_dispatch_lock() {
  if dispatch_lock_matches_current_attempt; then
    rm -rf "${DISPATCH_LOCK_DIR}"
    DISPATCH_LOCK_ACQUIRED=0
  fi
}

lock_file_matches_current_attempt() {
  [[ -f "${LOCK_PATH}" ]] || return 1
  [[ -n "${ATTEMPT_ID:-}" ]] || return 1
  grep -qx "attempt_id: ${ATTEMPT_ID}" "${LOCK_PATH}" 2>/dev/null
}

on_exit() {
  local code="$?"
  if [[ "${code}" -eq 0 ]]; then
    release_dispatch_lock
    return 0
  fi
  if [[ -d "${RUN_DIR}" ]]; then
    mkdir -p "${DIAGNOSTICS_DIR}"
    local stamp
    stamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    {
      echo "# Dispatch Failure"
      echo
      echo "- run_id: ${RUN_ID}"
      echo "- task_id: ${TASK_ID}"
      echo "- exit_code: ${code}"
      echo "- status_updated: ${STATUS_UPDATED}"
      echo "- attempt_id: ${ATTEMPT_ID:-}"
      echo "- lock_path: ${LOCK_PATH}"
      echo "- dispatch_lock_dir: ${DISPATCH_LOCK_DIR}"
      echo "- time: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    } > "${DIAGNOSTICS_DIR}/dispatch-failure-${TASK_ID}-${stamp}.md"
  fi
  if [[ "${STATUS_UPDATED}" == "0" ]] && lock_file_matches_current_attempt; then
    rm -f "${LOCK_PATH}"
  fi
  if [[ "${KEEP_DISPATCH_LOCK_ON_EXIT}" != "1" ]]; then
    release_dispatch_lock
  fi
  return 0
}

trap on_exit EXIT

if [[ ! -d "${TASK_DIR}" ]]; then
  echo "task not found: ${TASK_DIR}" >&2
  exit 2
fi

if [[ ! -f "${STATUS_PATH}" ]]; then
  echo "STATUS.json not found: ${STATUS_PATH}" >&2
  exit 2
fi

case "${RDO_WORKER_BACKEND}" in
  plain|tmux) ;;
  *)
    echo "invalid RDO_WORKER_BACKEND: ${RDO_WORKER_BACKEND} (expected plain or tmux)" >&2
    exit 2
    ;;
esac

if [[ "${RDO_WORKER_BACKEND}" == "tmux" ]] && ! command -v tmux >/dev/null 2>&1; then
  echo "RDO_WORKER_BACKEND=tmux requires tmux, but tmux was not found" >&2
  exit 2
fi

if ! [[ "${RDO_TMUX_WAIT_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "RDO_TMUX_WAIT_TIMEOUT_SECONDS must be a non-negative integer" >&2
  exit 2
fi

ATTEMPT_SEQ="$(find "${TASK_DIR}/attempts" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
ATTEMPT_NUM="$(printf "%03d" "$((ATTEMPT_SEQ + 1))")"
ATTEMPT_ID="A${ATTEMPT_NUM}-claude-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(3))
PY
)"
ATTEMPT_DIR="${TASK_DIR}/attempts/${ATTEMPT_ID}"
RUN_SHORT="$(printf "%s" "${RUN_ID}" | sanitize_name | cut -c1-18)"
TASK_SHORT="$(printf "%s" "${TASK_ID}" | sanitize_name | cut -c1-32)"
ATTEMPT_SHORT="$(printf "%s" "${ATTEMPT_ID}" | sanitize_name | cut -c1-18)"
TMUX_SESSION="$(printf "%s-%s-%s-%s" "${RDO_TMUX_SESSION_PREFIX}" "${RUN_SHORT:-run}" "${TASK_SHORT:-task}" "${ATTEMPT_SHORT:-attempt}" | sanitize_name | cut -c1-100)"
TMUX_ATTACH_COMMAND="tmux attach -t ${TMUX_SESSION}"

if ! mkdir "${DISPATCH_LOCK_DIR}" 2>/dev/null; then
  echo "task already has active dispatch lock: ${DISPATCH_LOCK_DIR}" >&2
  exit 3
fi
DISPATCH_LOCK_ACQUIRED=1
{
  echo "owner: dispatch"
  echo "pid: $$"
  echo "created_at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "command: $0 $RUN_ID $TASK_ID"
  echo "attempt_id: ${ATTEMPT_ID}"
  echo "backend: ${RDO_WORKER_BACKEND}"
} > "${DISPATCH_LOCK_DIR}/owner"
printf "%s\n" "${ATTEMPT_ID}" > "${DISPATCH_LOCK_DIR}/attempt_id"
printf "%s\n" "$$" > "${DISPATCH_LOCK_DIR}/pid"
if [[ "${RDO_WORKER_BACKEND}" == "tmux" ]]; then
  printf "%s\n" "${TMUX_SESSION}" > "${DISPATCH_LOCK_DIR}/tmux_session"
  printf "%s\n" "${TMUX_ATTACH_COMMAND}" > "${DISPATCH_LOCK_DIR}/attach_command"
fi

python3 - "$STATUS_PATH" "$FSM_PATH" <<'PY'
import json
import sys

status_path, fsm_path = sys.argv[1:3]
status = json.load(open(status_path, encoding="utf-8"))
fsm = json.load(open(fsm_path, encoding="utf-8"))
state = status.get("state")
allowed = fsm["transitions"].get(state, {}).get("running", [])
if "dispatch" not in allowed:
    raise SystemExit(f"illegal dispatch transition: {state!r} -> 'running'")
PY

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
if [[ "${WORKTREE_REL}" = /* ]]; then
  WORKTREE_PATH="${WORKTREE_REL}"
else
  WORKTREE_PATH="${REPO_ROOT}/${WORKTREE_REL}"
fi

mkdir -p "${ATTEMPT_DIR}"

{
  echo "owner: dispatch"
  echo "pid: $$"
  echo "created_at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "command: $0 $RUN_ID $TASK_ID"
  echo "attempt_id: ${ATTEMPT_ID}"
  echo "backend: ${RDO_WORKER_BACKEND}"
} > "${LOCK_PATH}"

if [[ "${DISPATCH_DRY_RUN}" != "1" ]]; then
  if [[ ! -d "${WORKTREE_PATH}" ]]; then
    if git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
      git -C "${REPO_ROOT}" worktree add "${WORKTREE_PATH}" "${BRANCH}"
    else
      git -C "${REPO_ROOT}" worktree add -b "${BRANCH}" "${WORKTREE_PATH}" HEAD
    fi
  fi
fi

python3 - "$ATTEMPT_DIR/ATTEMPT.json" "$ATTEMPT_ID" "$TASK_ID" "$CLAUDE_AGENT_NAME" "$CLAUDE_SESSION_ID" "$CLAUDE_CODE_CMD" "$WORKTREE_PATH" "$RDO_WORKER_BACKEND" "$TMUX_SESSION" "$TMUX_ATTACH_COMMAND" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, attempt_id, task_id, agent_name, session_id, command, cwd, backend, tmux_session, attach_command = sys.argv[1:11]
runtime = {
    "backend": backend,
    "model": os.environ.get("CLAUDE_MODEL"),
    "cli": command.split()[0] if command.split() else command,
    "command": command,
    "cwd": cwd,
}
if backend == "tmux":
    runtime["tmux_session"] = tmux_session
    runtime["attach_command"] = attach_command
payload = {
    "attempt_id": attempt_id,
    "task_id": task_id,
    "agent": "claude-code",
    "agent_name": agent_name,
    "session_id": session_id,
    "state": "created",
    "handoff_valid": None,
    "handoff_state": None,
    "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "ended_at": None,
    "exit_code": None,
    "runtime": runtime,
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
  echo "## Protocol File Paths"
  echo
  echo "You are running in this worktree:"
  echo
  echo "- WORKTREE_PATH: ${WORKTREE_PATH}"
  echo
  echo "The orchestration protocol files are outside the worktree. Write to these absolute paths:"
  echo
  echo "- TASK_DIR: ${TASK_DIR}"
  echo "- STATUS_PATH: ${STATUS_PATH}"
  echo "- EVIDENCE_PATH: ${TASK_DIR}/EVIDENCE.md"
  echo "- HANDOFF_PATH: ${TASK_DIR}/HANDOFF.md"
  echo "- ATTEMPT_DIR: ${ATTEMPT_DIR}"
  echo "- LOGS_DIR: ${TASK_DIR}/logs"
  echo
  echo "Do not create alternate STATUS/EVIDENCE/HANDOFF files inside the worktree."
  echo
  echo "## Protocol Reminders"
  echo
  echo "- You may only transition STATUS.json from running to review or blocked."
  echo "- Append the matching state_history entry: running -> review|blocked with actor claude-code."
  echo "- If blocked, blocker_type must be one of: needs_coordinator, needs_user, environment, budget, irrecoverable."
  echo "- Do not write approved, merged, failed, or changes_requested."
  echo "- Remove RDO_TEMPLATE markers from EVIDENCE.md or HANDOFF.md before ending."
  echo "- Write substantive EVIDENCE.md and HANDOFF.md before ending."
  echo "- Keep code changes inside allowed_paths."
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
status["needs_coordinator"] = False
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
with open(status_path, "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2)
    handle.write("\n")
PY
STATUS_UPDATED=1
python3 - "$ATTEMPT_DIR/ATTEMPT.json" <<'PY'
import json
import sys

path = sys.argv[1]
attempt = json.load(open(path, encoding="utf-8"))
attempt["state"] = "running"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(attempt, handle, indent=2)
    handle.write("\n")
PY
append_event "task_dispatched"

if [[ "${DISPATCH_DRY_RUN}" == "1" ]]; then
  echo "dry run: prompt written to ${ATTEMPT_DIR}/prompt.md" | tee "${ATTEMPT_DIR}/result.md"
  touch "${ATTEMPT_DIR}/transcript.log"
  EXIT_CODE=0
  EXIT_CODE_RAW="0"
else
  if [[ "${RDO_WORKER_BACKEND}" == "plain" ]]; then
    set +e
    (cd "${WORKTREE_PATH}" && ${CLAUDE_CODE_CMD} < "${ATTEMPT_DIR}/prompt.md") \
      > "${ATTEMPT_DIR}/transcript.log" 2>&1
    EXIT_CODE=$?
    set -e
    EXIT_CODE_RAW="${EXIT_CODE}"
  else
    EXIT_CODE_FILE="${ATTEMPT_DIR}/exit_code"
    RUNNER_PATH="${ATTEMPT_DIR}/run-worker.sh"
    DONE_SIGNAL="rdo-done-${TMUX_SESSION}"
    rm -f "${EXIT_CODE_FILE}" "${EXIT_CODE_FILE}.tmp"
    python3 - "$RUNNER_PATH" "$WORKTREE_PATH" "$CLAUDE_CODE_CMD" "$ATTEMPT_DIR/prompt.md" "$ATTEMPT_DIR/transcript.log" "$EXIT_CODE_FILE" "$DONE_SIGNAL" "$RDO_TMUX_KEEP_SESSION" <<'PY'
import os
import shlex
import sys
from pathlib import Path

runner_path, worktree_path, command, prompt_path, transcript_path, exit_code_file, done_signal, keep_session = sys.argv[1:9]
content = f"""#!/usr/bin/env bash
set +e
WORKTREE_PATH={shlex.quote(worktree_path)}
CLAUDE_CODE_CMD={shlex.quote(command)}
PROMPT_PATH={shlex.quote(prompt_path)}
TRANSCRIPT_PATH={shlex.quote(transcript_path)}
EXIT_CODE_FILE={shlex.quote(exit_code_file)}
DONE_SIGNAL={shlex.quote(done_signal)}
KEEP_SESSION={shlex.quote(keep_session)}

finish() {{
  local rc="$?"
  local tmp="${{EXIT_CODE_FILE}}.tmp"
  echo "${{rc}}" > "${{tmp}}"
  mv "${{tmp}}" "${{EXIT_CODE_FILE}}"
  tmux wait-for -S "${{DONE_SIGNAL}}" 2>/dev/null || true
  if [[ "${{KEEP_SESSION}}" == "1" ]]; then
    echo
    echo "Worker finished with exit code ${{rc}}."
    echo "Press Ctrl-D or run exit to close this tmux session."
    exec bash -l
  fi
  exit "${{rc}}"
}}
trap finish EXIT

cd "${{WORKTREE_PATH}}" || exit 127
set -o pipefail
eval "${{CLAUDE_CODE_CMD}}" < "${{PROMPT_PATH}}" 2>&1 | tee "${{TRANSCRIPT_PATH}}"
exit "${{PIPESTATUS[0]}}"
"""
Path(runner_path).write_text(content, encoding="utf-8")
os.chmod(runner_path, 0o755)
PY
    TMUX_COMMAND="$(python3 - "$RUNNER_PATH" <<'PY'
import shlex
import sys

print("exec " + shlex.quote(sys.argv[1]))
PY
)"
    tmux new-session -d -s "${TMUX_SESSION}" "${TMUX_COMMAND}"
    WAIT_START="$(date +%s)"
    while [[ ! -f "${EXIT_CODE_FILE}" ]]; do
      if [[ "${RDO_TMUX_WAIT_TIMEOUT_SECONDS}" != "0" ]]; then
        NOW="$(date +%s)"
        if (( NOW - WAIT_START >= RDO_TMUX_WAIT_TIMEOUT_SECONDS )); then
          KEEP_DISPATCH_LOCK_ON_EXIT=1
          write_tmux_timeout_diagnostics
          exit 5
        fi
      fi
      sleep 1
    done
    EXIT_CODE_RAW="$(cat "${EXIT_CODE_FILE}" 2>/dev/null || true)"
    if [[ "${RDO_TMUX_KEEP_SESSION}" != "1" ]]; then
      tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
    fi
    if [[ "${EXIT_CODE_RAW}" =~ ^[0-9]+$ ]]; then
      EXIT_CODE="${EXIT_CODE_RAW}"
    else
      EXIT_CODE=0
    fi
  fi
  {
    echo "# Worker Result"
    echo
    echo "exit_code: ${EXIT_CODE_RAW}"
  } > "${ATTEMPT_DIR}/result.md"
fi

set +e
python3 - "$STATUS_PATH" "$ATTEMPT_ID" "$TASK_DIR" "$ATTEMPT_DIR/ATTEMPT.json" "$EXIT_CODE_RAW" <<'PY'
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

TEMPLATE_MARKERS = {
    "EVIDENCE.md": "<!-- RDO_TEMPLATE: EVIDENCE -->",
    "HANDOFF.md": "<!-- RDO_TEMPLATE: HANDOFF -->",
}
BLOCKER_TYPES = {"needs_coordinator", "needs_user", "environment", "budget", "irrecoverable"}

def substantive(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return False
    marker = TEMPLATE_MARKERS.get(path.name)
    return not (marker and marker in text)

def parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

status_path, attempt_id, task_dir, attempt_path, exit_code_raw = sys.argv[1:6]
try:
    exit_code = int(exit_code_raw)
    exit_code_valid = True
except (TypeError, ValueError):
    exit_code = None
    exit_code_valid = False
status = json.load(open(status_path, encoding="utf-8"))
state = status.get("state")
valid_state = state in {"review", "blocked"}
valid_attempt = status.get("current_attempt_id") == attempt_id
valid_previous = status.get("previous_state") == "running"
history = status.get("state_history") if isinstance(status.get("state_history"), list) else []
last_transition = history[-1] if history else {}
valid_history = (
    isinstance(last_transition, dict)
    and last_transition.get("from") == "running"
    and last_transition.get("to") == state
    and last_transition.get("actor") == "claude-code"
    and parse_iso(last_transition.get("at")) is not None
)
handoff_ok = substantive(Path(task_dir, "HANDOFF.md"))
evidence_ok = substantive(Path(task_dir, "EVIDENCE.md"))
if state == "review":
    artifacts_ok = handoff_ok and evidence_ok
    exit_ok = exit_code_valid and exit_code == 0
elif state == "blocked":
    artifacts_ok = handoff_ok and status.get("blocker_type") in BLOCKER_TYPES and bool(status.get("blocking_reason"))
    exit_ok = exit_code_valid
else:
    artifacts_ok = False
    exit_ok = False

attempt = json.load(open(attempt_path, encoding="utf-8"))
attempt["ended_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
attempt["exit_code"] = exit_code

if not (exit_code_valid and valid_state and valid_attempt and valid_previous and valid_history and artifacts_ok and exit_ok):
    attempt["state"] = "invalid_handoff"
    attempt["handoff_valid"] = False
    attempt["handoff_state"] = None
    with open(attempt_path, "w", encoding="utf-8") as handle:
        json.dump(attempt, handle, indent=2)
        handle.write("\n")
    print("worker_exit_without_valid_status", file=sys.stderr)
    print(f"state={state!r} current_attempt_id={status.get('current_attempt_id')!r}", file=sys.stderr)
    print(f"exit_code_raw={exit_code_raw!r} exit_code={exit_code!r} exit_code_valid={exit_code_valid!r} exit_ok={exit_ok!r}", file=sys.stderr)
    print(f"valid_previous={valid_previous!r}", file=sys.stderr)
    print(f"valid_history={valid_history!r}", file=sys.stderr)
    raise SystemExit(4)

attempt["state"] = "completed"
attempt["handoff_valid"] = True
attempt["handoff_state"] = state
with open(attempt_path, "w", encoding="utf-8") as handle:
    json.dump(attempt, handle, indent=2)
    handle.write("\n")
PY
VALIDATION_CODE=$?
set -e
if [[ "${VALIDATION_CODE}" -ne 0 ]]; then
  append_event "worker_exit_without_valid_status"
  exit "${VALIDATION_CODE}"
fi

FINAL_STATE="$(python3 - "$STATUS_PATH" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("state", ""))
PY
)"
if [[ "${FINAL_STATE}" == "review" ]]; then
  append_event "worker_review_ready"
elif [[ "${FINAL_STATE}" == "blocked" ]]; then
  append_event "worker_blocked"
fi
