#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: scripts/dispatch_claude.sh <run-id> <task-id>" >&2
  exit 2
fi

RUN_ID="$1"
TASK_ID="$2"
DISPATCH_DRY_RUN="${DISPATCH_DRY_RUN:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROTOCOL_CLI="${SCRIPT_DIR}/protocol_cli.py"
CONFIG_CLI="${SCRIPT_DIR}/config_cli.py"
DISPATCH_ASSETS="${SCRIPT_DIR}/dispatch_assets.py"
AGENT_BACKEND_CLI="${SCRIPT_DIR}/agent_backend_cli.py"
BACKEND_GOVERNANCE_CLI="${SCRIPT_DIR}/backend_governance_cli.py"
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
  LC_ALL=C tr -c 'A-Za-z0-9_.-' '-' | sed 's/^-*//; s/-*$//'
}

normalize_bool() {
  case "$(printf "%s" "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
      printf "1"
      ;;
    0|false|no|off)
      printf "0"
      ;;
    *)
      return 1
      ;;
  esac
}

write_tmux_timeout_diagnostics() {
  python3 "${PROTOCOL_CLI}" write-tmux-timeout-diagnostics \
    --run-dir "${RUN_DIR}" \
    --run-id "${RUN_ID}" \
    --task-id "${TASK_ID}" \
    --attempt-id "${ATTEMPT_ID:-}" \
    --tmux-session "${TMUX_SESSION:-}" \
    --attach-command "${TMUX_ATTACH_COMMAND:-}" \
    --timeout-seconds "${RDO_TMUX_WAIT_TIMEOUT_SECONDS}" \
    --dispatch-lock-dir "${DISPATCH_LOCK_DIR}" \
    --attempt-dir "${ATTEMPT_DIR:-}"
}

append_event() {
  local event_name="$1"
  python3 "${PROTOCOL_CLI}" append-event \
    --run-dir "${RUN_DIR}" \
    --run-id "${RUN_ID}" \
    --task-id "${TASK_ID}" \
    --attempt-id "${ATTEMPT_ID}" \
    --event-name "${event_name}" \
    --agent-name "${RDO_WORKER_AGENT_NAME}" \
    --worker-backend "${RDO_WORKER_BACKEND}" \
    --status-path "${STATUS_PATH}"
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
    python3 "${PROTOCOL_CLI}" write-dispatch-diagnostics \
      --run-dir "${RUN_DIR}" \
      --run-id "${RUN_ID}" \
      --task-id "${TASK_ID}" \
      --attempt-id "${ATTEMPT_ID:-}" \
      --exit-code "${code}" \
      --status-updated "${STATUS_UPDATED}" \
      --lock-path "${LOCK_PATH}" \
      --dispatch-lock-dir "${DISPATCH_LOCK_DIR}" || true
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

set +e
CONFIG_ENV="$(python3 "${CONFIG_CLI}" export-env --no-env --prefix CONFIG_)"
CONFIG_STATUS=$?
set -e
if [[ "${CONFIG_STATUS}" -ne 0 ]]; then
  exit "${CONFIG_STATUS}"
fi
eval "${CONFIG_ENV}"

: "${RDO_WORKER_COMMAND:=${CLAUDE_CODE_CMD:-${CONFIG_RDO_WORKER_COMMAND}}}"
: "${RDO_WORKER_AGENT_NAME:=${CLAUDE_AGENT_NAME:-${CONFIG_RDO_WORKER_AGENT_NAME}}}"
: "${RDO_BACKEND_SESSION_ID:=${CLAUDE_SESSION_ID:-${CONFIG_RDO_BACKEND_SESSION_ID}}}"
: "${RDO_WORKER_BACKEND:=${CONFIG_RDO_WORKER_BACKEND}}"
: "${RDO_PERMISSION_MODE:=${CONFIG_RDO_PERMISSION_MODE}}"
: "${RDO_RUNTIME_BACKEND:=${CONFIG_RDO_RUNTIME_BACKEND}}"
: "${RDO_IO_MODE:=${CONFIG_RDO_IO_MODE}}"
: "${RDO_TMUX_SESSION_PREFIX:=${CONFIG_RDO_TMUX_SESSION_PREFIX}}"
: "${RDO_TMUX_KEEP_SESSION:=${CONFIG_RDO_TMUX_KEEP_SESSION}}"
: "${RDO_TMUX_WAIT_TIMEOUT_SECONDS:=${CONFIG_RDO_TMUX_WAIT_TIMEOUT_SECONDS}}"
: "${RDO_ATTEMPT_PHASE:=auto}"

STATUS_STATE="$(python3 - "${STATUS_PATH}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("state", ""))
PY
)"
if [[ "${RDO_ATTEMPT_PHASE}" == "auto" ]]; then
  case "${STATUS_STATE}" in
    pending|blocked|changes_requested) RDO_ATTEMPT_PHASE="planning" ;;
    strategy_review) RDO_ATTEMPT_PHASE="execution" ;;
    *) echo "cannot auto-detect dispatch phase from state ${STATUS_STATE}" >&2; exit 2 ;;
  esac
fi
case "${RDO_ATTEMPT_PHASE}" in
  planning|execution) ;;
  *) echo "RDO_ATTEMPT_PHASE must be planning or execution" >&2; exit 2 ;;
esac

STRATEGY_ID=""
STRATEGY_SHA256=""
STRATEGY_PATH=""
if [[ "${RDO_ATTEMPT_PHASE}" == "execution" ]]; then
  STRATEGY_INFO="$(PYTHONPATH="${SCRIPT_DIR}" python3 - "${TASK_DIR}" <<'PY'
import json, sys
from pathlib import Path
from strategy import load_approved_strategy
task = Path(sys.argv[1])
strategy, review = load_approved_strategy(task)
print(json.dumps({"id": strategy["strategy_id"], "sha": review["strategy_sha256"], "revision": strategy["revision"], "wall": strategy["global_budget"]["wall_seconds"]}))
PY
)" || exit 2
  STRATEGY_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "${STRATEGY_INFO}")"
  STRATEGY_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha"])' "${STRATEGY_INFO}")"
  STRATEGY_REVISION="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["revision"])' "${STRATEGY_INFO}")"
  ATTEMPT_TIMEOUT_SECONDS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["wall"])' "${STRATEGY_INFO}")"
  STRATEGY_PATH="${TASK_DIR}/strategy/STRATEGY-v$(printf '%03d' "${STRATEGY_REVISION}").json"
else
  ATTEMPT_TIMEOUT_SECONDS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_wall_seconds"])' "${TASK_DIR}/EXECUTION_POLICY.json")"
fi

if ! RDO_TMUX_KEEP_SESSION="$(normalize_bool "${RDO_TMUX_KEEP_SESSION}")"; then
  echo "RDO_TMUX_KEEP_SESSION must be boolean: 1/0/true/false/yes/no/on/off" >&2
  exit 2
fi

# Compatibility: v0.2 used RDO_WORKER_BACKEND=plain|tmux for runtime selection.
if [[ "${RDO_WORKER_BACKEND}" == "plain" || "${RDO_WORKER_BACKEND}" == "tmux" ]]; then
  RDO_RUNTIME_BACKEND="${RDO_WORKER_BACKEND}"
  RDO_WORKER_BACKEND="claude-code"
fi

if [[ "${RDO_RUNTIME_BACKEND}" == "tmux" && "${RDO_IO_MODE}" == "machine" ]]; then
  RDO_IO_MODE="human"
fi

case "${RDO_WORKER_BACKEND}" in
  claude-code|codex|opencode|kimi-code) ;;
  *)
    echo "invalid RDO_WORKER_BACKEND: ${RDO_WORKER_BACKEND} (expected claude-code, codex, opencode, kimi-code)" >&2
    exit 2
    ;;
esac

case "${RDO_RUNTIME_BACKEND}" in
  plain|tmux) ;;
  *)
    echo "invalid RDO_RUNTIME_BACKEND: ${RDO_RUNTIME_BACKEND} (expected plain or tmux)" >&2
    exit 2
    ;;
esac

case "${RDO_IO_MODE}" in
  machine|human) ;;
  *)
    echo "invalid RDO_IO_MODE: ${RDO_IO_MODE} (expected machine or human)" >&2
    exit 2
    ;;
esac

case "${RDO_PERMISSION_MODE}" in
  default|auto|yolo) ;;
  *)
    echo "invalid RDO_PERMISSION_MODE: ${RDO_PERMISSION_MODE} (expected default, auto, yolo)" >&2
    exit 2
    ;;
esac

if [[ "${RDO_RUNTIME_BACKEND}" == "plain" && "${RDO_IO_MODE}" != "machine" ]]; then
  echo "plain runtime only supports machine IO mode in v0.3" >&2
  exit 2
fi
if [[ "${RDO_RUNTIME_BACKEND}" == "tmux" && "${RDO_IO_MODE}" != "human" ]]; then
  echo "tmux runtime only supports human IO mode in v0.3" >&2
  exit 2
fi

if [[ "${RDO_RUNTIME_BACKEND}" == "tmux" ]] && ! command -v tmux >/dev/null 2>&1; then
  echo "RDO_RUNTIME_BACKEND=tmux requires tmux, but tmux was not found" >&2
  exit 2
fi

if ! [[ "${RDO_TMUX_WAIT_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "RDO_TMUX_WAIT_TIMEOUT_SECONDS must be a non-negative integer" >&2
  exit 2
fi

if [[ -z "${RDO_WORKER_COMMAND}" ]]; then
  python3 "${AGENT_BACKEND_CLI}" command \
    --backend "${RDO_WORKER_BACKEND}" \
    --io-mode "${RDO_IO_MODE}" \
    --permission-mode "${RDO_PERMISSION_MODE}" \
    --cwd "${REPO_ROOT}" \
    --prompt "" \
    --agent-name "${RDO_WORKER_AGENT_NAME}" >/dev/null
fi

BACKEND_PROFILE_COMPILE_ARGS=(
  compile
  --repo-root "${REPO_ROOT}"
  --task-dir "${TASK_DIR}"
  --backend "${RDO_WORKER_BACKEND}"
  --phase "${RDO_ATTEMPT_PHASE}"
)
if [[ -n "${STRATEGY_PATH}" ]]; then
  BACKEND_PROFILE_COMPILE_ARGS+=(--strategy "${STRATEGY_PATH}")
fi
BACKEND_PROFILE_JSON="$(python3 "${BACKEND_GOVERNANCE_CLI}" "${BACKEND_PROFILE_COMPILE_ARGS[@]}")" || exit 2
if [[ -n "${RDO_WORKER_COMMAND}" && "${RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE:-0}" != "1" ]]; then
  HARD_BACKEND_CONTROLS="$(python3 -c 'import json,sys; print(sum(1 for item in json.loads(sys.argv[1])["controls"] if item.get("hard") and item.get("enforcement") != "observed"))' "${BACKEND_PROFILE_JSON}")"
  if [[ "${HARD_BACKEND_CONTROLS}" != "0" ]]; then
    echo "worker.command cannot receive ${HARD_BACKEND_CONTROLS} required backend governance control(s); use the registered backend command" >&2
    exit 2
  fi
fi

ATTEMPT_SEQ="$(find "${TASK_DIR}/attempts" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
ATTEMPT_NUM="$(printf "%03d" "$((ATTEMPT_SEQ + 1))")"
ATTEMPT_BACKEND_SHORT="$(printf "%s" "${RDO_WORKER_BACKEND}" | sed 's/-code$//' | sanitize_name | cut -c1-12)"
ATTEMPT_ID="A${ATTEMPT_NUM}-${ATTEMPT_BACKEND_SHORT:-worker}-$(python3 - <<'PY'
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
  echo "worker_backend: ${RDO_WORKER_BACKEND}"
  echo "runtime_backend: ${RDO_RUNTIME_BACKEND}"
  echo "io_mode: ${RDO_IO_MODE}"
} > "${DISPATCH_LOCK_DIR}/owner"
printf "%s\n" "${ATTEMPT_ID}" > "${DISPATCH_LOCK_DIR}/attempt_id"
printf "%s\n" "$$" > "${DISPATCH_LOCK_DIR}/pid"
if [[ "${RDO_RUNTIME_BACKEND}" == "tmux" ]]; then
  printf "%s\n" "${TMUX_SESSION}" > "${DISPATCH_LOCK_DIR}/tmux_session"
  printf "%s\n" "${TMUX_ATTACH_COMMAND}" > "${DISPATCH_LOCK_DIR}/attach_command"
fi

python3 "${PROTOCOL_CLI}" check-dispatch-transition \
  --status-path "${STATUS_PATH}" \
  --fsm-path "${FSM_PATH}" \
  --phase "${RDO_ATTEMPT_PHASE}"

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

BACKEND_MATERIALIZED_JSON="$(python3 "${BACKEND_GOVERNANCE_CLI}" materialize \
  --profile-json "${BACKEND_PROFILE_JSON}" \
  --runtime-dir "${ATTEMPT_DIR}/runtime")" || exit 2
BACKEND_PROFILE_PATH="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["profile_path"])' "${BACKEND_MATERIALIZED_JSON}")"
BACKEND_PROFILE_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["profile_sha256"])' "${BACKEND_MATERIALIZED_JSON}")"
BACKEND_SETTINGS_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("settings_sha256") or "")' "${BACKEND_MATERIALIZED_JSON}")"

for artifact in EVIDENCE.md HANDOFF.md HANDOFF.json; do
  if [[ -f "${TASK_DIR}/${artifact}" ]]; then
    cp "${TASK_DIR}/${artifact}" "${ATTEMPT_DIR}/preexisting-${artifact}"
  fi
  cp "${SKILL_ROOT}/templates/task/${artifact}" "${TASK_DIR}/${artifact}"
done
rm -f "${TASK_DIR}/HANDOFF.complete"

{
  echo "owner: dispatch"
  echo "pid: $$"
  echo "created_at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "command: $0 $RUN_ID $TASK_ID"
  echo "attempt_id: ${ATTEMPT_ID}"
  echo "worker_backend: ${RDO_WORKER_BACKEND}"
  echo "runtime_backend: ${RDO_RUNTIME_BACKEND}"
  echo "io_mode: ${RDO_IO_MODE}"
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

python3 "${DISPATCH_ASSETS}" render-prompt \
  --output "${ATTEMPT_DIR}/prompt.md" \
  --worktree-path "${WORKTREE_PATH}" \
  --task-dir "${TASK_DIR}" \
  --status-path "${STATUS_PATH}" \
  --attempt-dir "${ATTEMPT_DIR}" \
  --worker-backend "${RDO_WORKER_BACKEND}" \
  --agent-name "${RDO_WORKER_AGENT_NAME}" \
  --phase "${RDO_ATTEMPT_PHASE}" \
  --strategy-path "${STRATEGY_PATH}"

PROMPT_TRANSPORT="stdin"
PROMPT_SUBMIT_KEY=""
PROMPT_POST_PASTE_DELAY_MS="0"
if [[ -z "${RDO_WORKER_COMMAND}" ]]; then
  BACKEND_COMMAND_JSON="$(python3 "${AGENT_BACKEND_CLI}" command \
    --backend "${RDO_WORKER_BACKEND}" \
    --io-mode "${RDO_IO_MODE}" \
    --permission-mode "${RDO_PERMISSION_MODE}" \
    --cwd "${WORKTREE_PATH}" \
    --prompt-path "${ATTEMPT_DIR}/prompt.md" \
    --agent-name "${RDO_WORKER_AGENT_NAME}" \
    --backend-profile "${BACKEND_PROFILE_PATH}" \
    --json)"
  RDO_WORKER_COMMAND="$(python3 - <<'PY' "${BACKEND_COMMAND_JSON}"
import json, sys
print(json.loads(sys.argv[1])["command"])
PY
)"
  PROMPT_TRANSPORT="$(python3 - <<'PY' "${BACKEND_COMMAND_JSON}"
import json, sys
print(json.loads(sys.argv[1])["prompt_transport"])
PY
)"
  PROMPT_SUBMIT_KEY="$(python3 - <<'PY' "${BACKEND_COMMAND_JSON}"
import json, sys
print(json.loads(sys.argv[1]).get("submit_key") or "")
PY
)"
  PROMPT_POST_PASTE_DELAY_MS="$(python3 - <<'PY' "${BACKEND_COMMAND_JSON}"
import json, sys
print(json.loads(sys.argv[1]).get("post_paste_delay_ms") or 0)
PY
)"
fi

ORIGINAL_WORKER_COMMAND="${RDO_WORKER_COMMAND}"
SUPERVISOR_RESULT="${ATTEMPT_DIR}/supervisor-result.json"
RDO_WORKER_COMMAND="$(python3 - "${SCRIPT_DIR}/supervise_attempt.py" "${ATTEMPT_TIMEOUT_SECONDS}" "${SUPERVISOR_RESULT}" "${WORKTREE_PATH}" "${ORIGINAL_WORKER_COMMAND}" "${STRATEGY_ID}" "${STRATEGY_SHA256}" <<'PY'
import shlex, sys
script, timeout, result, cwd, command, strategy_id, strategy_sha256 = sys.argv[1:]
print(" ".join([
    shlex.quote(sys.executable), shlex.quote(script),
    "--timeout-seconds", shlex.quote(timeout),
    "--result", shlex.quote(result),
    "--cwd", shlex.quote(cwd),
    "--strategy-id", shlex.quote(strategy_id),
    "--strategy-sha256", shlex.quote(strategy_sha256),
    "--shell-command", shlex.quote(command),
]))
PY
)"

python3 "${PROTOCOL_CLI}" create-attempt \
  --path "${ATTEMPT_DIR}/ATTEMPT.json" \
  --attempt-id "${ATTEMPT_ID}" \
  --task-id "${TASK_ID}" \
  --agent-name "${RDO_WORKER_AGENT_NAME}" \
  --session-id "${RDO_BACKEND_SESSION_ID}" \
  --worker-backend "${RDO_WORKER_BACKEND}" \
  --execution-mode "start" \
  --phase "${RDO_ATTEMPT_PHASE}" \
  --strategy-id "${STRATEGY_ID}" \
  --strategy-sha256 "${STRATEGY_SHA256}" \
  --backend-profile-sha256 "${BACKEND_PROFILE_SHA256}" \
  --backend-settings-sha256 "${BACKEND_SETTINGS_SHA256}" \
  --permission-mode "${RDO_PERMISSION_MODE}" \
  --io-mode "${RDO_IO_MODE}" \
  --command "${ORIGINAL_WORKER_COMMAND}" \
  --supervisor-command "${RDO_WORKER_COMMAND}" \
  --cwd "${WORKTREE_PATH}" \
  --backend "${RDO_RUNTIME_BACKEND}" \
  --tmux-session "${TMUX_SESSION}" \
  --attach-command "${TMUX_ATTACH_COMMAND}"

python3 "${PROTOCOL_CLI}" transition-running \
  --status-path "${STATUS_PATH}" \
  --fsm-path "${FSM_PATH}" \
  --attempt-id "${ATTEMPT_ID}" \
  --agent-name "${RDO_WORKER_AGENT_NAME}" \
  --session-id "${RDO_BACKEND_SESSION_ID}" \
  --worker-backend "${RDO_WORKER_BACKEND}" \
  --phase "${RDO_ATTEMPT_PHASE}"
STATUS_UPDATED=1
python3 "${PROTOCOL_CLI}" set-attempt-running \
  --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json"
append_event "task_dispatched"

if [[ "${DISPATCH_DRY_RUN}" != "1" ]]; then
  python3 "${SCRIPT_DIR}/worktree_fingerprint.py" \
    --worktree "${WORKTREE_PATH}" \
    --output "${ATTEMPT_DIR}/runtime/worktree-before.json"
fi

if [[ "${DISPATCH_DRY_RUN}" == "1" ]]; then
  echo "dry run: prompt written to ${ATTEMPT_DIR}/prompt.md" | tee "${ATTEMPT_DIR}/result.md"
  touch "${ATTEMPT_DIR}/transcript.log"
  EXIT_CODE=0
  EXIT_CODE_RAW="0"
else
  if [[ "${RDO_RUNTIME_BACKEND}" == "plain" ]]; then
    set +e
    (cd "${WORKTREE_PATH}" && eval "${RDO_WORKER_COMMAND}" < "${ATTEMPT_DIR}/prompt.md") \
      > "${ATTEMPT_DIR}/transcript.log" 2>&1
    EXIT_CODE=$?
    set -e
    EXIT_CODE_RAW="${EXIT_CODE}"
  else
    EXIT_CODE_FILE="${ATTEMPT_DIR}/exit_code"
    RUNNER_PATH="${ATTEMPT_DIR}/run-worker.sh"
    DONE_SIGNAL="rdo-done-${TMUX_SESSION}"
    rm -f "${EXIT_CODE_FILE}" "${EXIT_CODE_FILE}.tmp"
    python3 "${DISPATCH_ASSETS}" render-tmux-runner \
      --output "${RUNNER_PATH}" \
      --worktree-path "${WORKTREE_PATH}" \
      --command "${RDO_WORKER_COMMAND}" \
      --prompt-path "${ATTEMPT_DIR}/prompt.md" \
      --transcript-path "${ATTEMPT_DIR}/transcript.log" \
      --exit-code-file "${EXIT_CODE_FILE}" \
      --done-signal "${DONE_SIGNAL}" \
      --keep-session "${RDO_TMUX_KEEP_SESSION}" \
      --prompt-transport "${PROMPT_TRANSPORT}" \
      --submit-key "${PROMPT_SUBMIT_KEY}" \
      --post-paste-delay-ms "${PROMPT_POST_PASTE_DELAY_MS}"
    TMUX_COMMAND="$(python3 - "$RUNNER_PATH" <<'PY'
import shlex
import sys

print("exec " + shlex.quote(sys.argv[1]))
PY
)"
    tmux new-session -d -s "${TMUX_SESSION}" "${TMUX_COMMAND}"
    if [[ "${PROMPT_TRANSPORT}" != "stdin" ]]; then
      tmux pipe-pane -o -t "${TMUX_SESSION}" "cat >> '${ATTEMPT_DIR}/transcript.log'" || true
    fi
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

if [[ "${DISPATCH_DRY_RUN}" != "1" ]]; then
  python3 "${SCRIPT_DIR}/worktree_fingerprint.py" \
    --worktree "${WORKTREE_PATH}" \
    --output "${ATTEMPT_DIR}/runtime/worktree-after.json"
  if [[ "${RDO_ATTEMPT_PHASE}" == "planning" ]] && ! cmp -s "${ATTEMPT_DIR}/runtime/worktree-before.json" "${ATTEMPT_DIR}/runtime/worktree-after.json"; then
    echo "planning worker modified the task worktree" >> "${ATTEMPT_DIR}/transcript.log"
    EXIT_CODE=126
    EXIT_CODE_RAW="126"
  elif [[ "${RDO_ATTEMPT_PHASE}" == "execution" ]] && ! python3 "${SCRIPT_DIR}/worktree_policy_check.py" \
      --before "${ATTEMPT_DIR}/runtime/worktree-before.json" \
      --after "${ATTEMPT_DIR}/runtime/worktree-after.json" \
      --strategy "${STRATEGY_PATH}" \
      --policy "${TASK_DIR}/EXECUTION_POLICY.json" \
      > "${ATTEMPT_DIR}/runtime/worktree-policy-result.json"; then
    echo "execution worker modified paths outside the approved strategy" >> "${ATTEMPT_DIR}/transcript.log"
    EXIT_CODE=126
    EXIT_CODE_RAW="126"
  fi
fi

if [[ -f "${SUPERVISOR_RESULT}" ]] && python3 - "${SUPERVISOR_RESULT}" <<'PY'
import json, sys
raise SystemExit(0 if json.load(open(sys.argv[1], encoding="utf-8")).get("timed_out") else 1)
PY
then
  append_event "attempt_timed_out"
fi

set +e
python3 "${PROTOCOL_CLI}" validate-handoff \
  --status-path "${STATUS_PATH}" \
  --attempt-id "${ATTEMPT_ID}" \
  --task-dir "${TASK_DIR}" \
  --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json" \
  --exit-code-raw "${EXIT_CODE_RAW}"
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
elif [[ "${FINAL_STATE}" == "strategy_review" ]]; then
  append_event "strategy_review_ready"
elif [[ "${FINAL_STATE}" == "blocked" ]]; then
  append_event "worker_blocked"
fi
