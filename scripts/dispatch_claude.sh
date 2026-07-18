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
BACKEND_PREFLIGHT="${SCRIPT_DIR}/backend_preflight.py"
RESUME_CONTEXT_CLI="${SCRIPT_DIR}/resume_context.py"
TASK_BUDGET_CLI="${SCRIPT_DIR}/task_budget_cli.py"
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
PROCESS_CLEANUP_FAILED=0
SUPERVISOR_CLEANUP_FAILED=0
TMUX_WORKER_LAUNCHED=0

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

json_files_equal() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as left, open(sys.argv[2], encoding="utf-8") as right:
    raise SystemExit(0 if json.load(left) == json.load(right) else 1)
PY
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
    --execution-mode "${RDO_EXECUTION_MODE}" \
    --status-path "${STATUS_PATH}"
}

cleanup_attempt_processes() {
  [[ -n "${ATTEMPT_DIR:-}" ]] || return 2
  local result_path="${ATTEMPT_DIR}/runtime/CLEANUP.json"
  local temporary="${result_path}.tmp"
  mkdir -p "${ATTEMPT_DIR}/runtime"
  python3 "${PROTOCOL_CLI}" terminate-attempt-processes \
    --supervisor-state "${ATTEMPT_DIR}/runtime/supervisor.json" \
    > "${temporary}"
  local cleanup_code=$?
  mv "${temporary}" "${result_path}"
  return "${cleanup_code}"
}

write_tmux_identity_startup_failure() {
  local detail="$1"
  python3 - "${ATTEMPT_DIR}/runtime/STARTUP.json" "${detail}" <<'PY'
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "mode": "human",
    "state": "tui_startup_failed",
    "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "failure": {
        "code": "tmux_identity_receipt_failed",
        "message": sys.argv[2],
    },
    "startup_evidence": {"event": "tmux_identity_receipt_failed"},
}
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
}

run_tmux_worker_once() {
  TMUX_WORKER_LAUNCHED=0
  EXIT_CODE_FILE="${ATTEMPT_DIR}/exit_code"
  RUNNER_PATH="${ATTEMPT_DIR}/run-worker.sh"
  DONE_SIGNAL="rdo-done-${TMUX_SESSION}"
  rm -f "${EXIT_CODE_FILE}" "${EXIT_CODE_FILE}.tmp"
  python3 "${DISPATCH_ASSETS}" render-tmux-runner \
    --output "${RUNNER_PATH}" \
    --worktree-path "${WORKTREE_PATH}" \
    --command "${RDO_WORKER_COMMAND}" \
    --prompt-path "${ATTEMPT_DIR}/prompt.md" \
    --transcript-path "${TRANSCRIPT_PATH}" \
    --exit-code-file "${EXIT_CODE_FILE}" \
    --done-signal "${DONE_SIGNAL}" \
    --keep-session "${RDO_TMUX_KEEP_SESSION}" \
    --prompt-transport "${PROMPT_TRANSPORT}" \
    --submit-key "${PROMPT_SUBMIT_KEY}" \
    --post-paste-delay-ms "${PROMPT_POST_PASTE_DELAY_MS}" \
    --startup-path "${ATTEMPT_DIR}/runtime/STARTUP.json" \
    --startup-timeout-seconds "${RDO_STARTUP_TIMEOUT_SECONDS}" \
    --backend-id "${RDO_WORKER_BACKEND}"
  TMUX_COMMAND="$(python3 - "$RUNNER_PATH" <<'PY'
import shlex
import sys

print("exec " + shlex.quote(sys.argv[1]))
PY
)"
  # Create the tmux identity before starting the worker. This prevents a new
  # protocol attempt from running without a durable receipt for later control.
  tmux new-session -d -s "${TMUX_SESSION}" "sleep 2147483647"
  set +e
  TMUX_RECEIPT_ERROR="$(python3 "${SCRIPT_DIR}/tmux_lifecycle.py" record \
    --output "${ATTEMPT_DIR}/runtime/TMUX_SESSION.json" \
    --run-id "${RUN_ID}" \
    --task-id "${TASK_ID}" \
    --attempt-id "${ATTEMPT_ID}" \
    --session-name "${TMUX_SESSION}" 2>&1 >/dev/null)"
  TMUX_RECEIPT_CODE=$?
  set -e
  if [[ "${TMUX_RECEIPT_CODE}" -ne 0 ]]; then
    tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
    set +e
    write_tmux_identity_startup_failure "${TMUX_RECEIPT_ERROR:-tmux identity receipt could not be persisted}"
    set -e
    return 125
  fi
  TMUX_SESSION_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["session_id"])' "${ATTEMPT_DIR}/runtime/TMUX_SESSION.json")"
  if [[ "${PROMPT_TRANSPORT}" != "stdin" ]]; then
    tmux pipe-pane -o -t "${TMUX_SESSION_ID}" "cat >> '${TRANSCRIPT_PATH}'" || true
  fi
  if ! tmux respawn-pane -k -t "${TMUX_SESSION_ID}" "${TMUX_COMMAND}"; then
    tmux kill-session -t "${TMUX_SESSION_ID}" 2>/dev/null || true
    set +e
    write_tmux_identity_startup_failure "tmux could not start the worker in the receipt-bound session"
    set -e
    return 125
  fi
  TMUX_WORKER_LAUNCHED=1
  WAIT_START="$(date +%s)"
  while [[ ! -f "${EXIT_CODE_FILE}" ]]; do
    STARTUP_PROBE_MARKER="${ATTEMPT_DIR}/runtime/human-startup-probed"
    if [[ "${RDO_IO_MODE}" == "human" && ! -f "${STARTUP_PROBE_MARKER}" ]]; then
      PANE_SNAPSHOT="${ATTEMPT_DIR}/runtime/startup-pane.txt"
      tmux capture-pane -p -t "${TMUX_SESSION}" > "${PANE_SNAPSHOT}" 2>/dev/null || true
      set +e
      python3 "${SCRIPT_DIR}/human_startup_probe.py" \
        --startup-path "${ATTEMPT_DIR}/runtime/STARTUP.json" \
        --pane-path "${PANE_SNAPSHOT}" \
        --backend "${RDO_WORKER_BACKEND}" >/dev/null
      HUMAN_PROBE_CODE=$?
      set -e
      if [[ "${HUMAN_PROBE_CODE}" -eq 0 ]]; then
        touch "${STARTUP_PROBE_MARKER}"
        append_event "worker_waiting_for_user"
        echo "Worker is waiting for startup input; attach with: ${TMUX_ATTACH_COMMAND}" >&2
      elif [[ "${HUMAN_PROBE_CODE}" -eq 2 ]]; then
        touch "${STARTUP_PROBE_MARKER}"
        append_event "worker_startup_failed"
        set +e
        cleanup_attempt_processes
        HUMAN_CLEANUP_CODE=$?
        set -e
        if [[ "${HUMAN_CLEANUP_CODE}" -ne 0 ]]; then
          PROCESS_CLEANUP_FAILED=1
          exit 6
        fi
        tmux send-keys -t "${TMUX_SESSION}" C-c 2>/dev/null || true
        sleep 0.2
        tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
        if [[ ! -f "${EXIT_CODE_FILE}" ]]; then
          printf '125\n' > "${EXIT_CODE_FILE}.tmp"
          mv "${EXIT_CODE_FILE}.tmp" "${EXIT_CODE_FILE}"
        fi
      fi
    fi
    if [[ "${RDO_TMUX_WAIT_TIMEOUT_SECONDS}" != "0" ]]; then
      NOW="$(date +%s)"
      if (( NOW - WAIT_START >= RDO_TMUX_WAIT_TIMEOUT_SECONDS )); then
        python3 - "${ATTEMPT_DIR}/runtime/DISPATCH_TIMEOUT.json" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = dict(
    reason="tmux_wait_timeout",
    exit_code=124,
    timed_out=True,
    timeout_source="dispatcher_tmux_wait",
)
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
        write_tmux_timeout_diagnostics
        exit 5
      fi
    fi
    sleep 1
  done
  if [[ "${RDO_IO_MODE}" == "human" && -f "${TRANSCRIPT_PATH}" ]]; then
    set +e
    python3 "${SCRIPT_DIR}/human_startup_probe.py" \
      --startup-path "${ATTEMPT_DIR}/runtime/STARTUP.json" \
      --pane-path "${TRANSCRIPT_PATH}" \
      --backend "${RDO_WORKER_BACKEND}" >/dev/null
    set -e
  fi
  EXIT_CODE_RAW="$(cat "${EXIT_CODE_FILE}" 2>/dev/null || true)"
  if [[ "${RDO_TMUX_KEEP_SESSION}" != "1" ]]; then
    tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
  fi
  if [[ "${EXIT_CODE_RAW}" =~ ^[0-9]+$ ]]; then
    EXIT_CODE="${EXIT_CODE_RAW}"
  else
    EXIT_CODE=125
  fi
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
  local reconcile_code=0
  local cleanup_code=0
  if [[ "${code}" -eq 0 && \
        "${PROCESS_CLEANUP_FAILED}" != "1" && \
        "${SUPERVISOR_CLEANUP_FAILED}" != "1" ]]; then
    release_dispatch_lock
    return 0
  fi
  if [[ "${PROCESS_CLEANUP_FAILED}" == "1" || \
        ( "${RDO_RUNTIME_BACKEND:-}" == "tmux" && \
          "${TMUX_WORKER_LAUNCHED}" == "1" && \
          ( -z "${EXIT_CODE_FILE:-}" || ! -f "${EXIT_CODE_FILE}" ) ) ]]; then
    set +e
    cleanup_attempt_processes
    cleanup_code=$?
    set -e
    if [[ "${cleanup_code}" -ne 0 ]]; then
      PROCESS_CLEANUP_FAILED=1
    else
      PROCESS_CLEANUP_FAILED=0
    fi
  fi
  if [[ "${RDO_RUNTIME_BACKEND:-}" == "tmux" && -n "${TMUX_SESSION:-}" ]] && \
     tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
    tmux send-keys -t "${TMUX_SESSION}" C-c 2>/dev/null || true
    sleep 0.2
    tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
  fi
  if [[ -n "${ATTEMPT_ID:-}" && -n "${ATTEMPT_DIR:-}" && \
        -d "${ATTEMPT_DIR}" && -f "${STATUS_PATH}" ]]; then
    set +e
    python3 "${PROTOCOL_CLI}" reconcile-dispatch-exit \
      --status-path "${STATUS_PATH}" \
      --task-dir "${TASK_DIR}" \
      --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json" \
      --attempt-id "${ATTEMPT_ID}" \
      --startup-path "${ATTEMPT_DIR}/runtime/STARTUP.json" \
      --supervisor-result "${SUPERVISOR_RESULT:-}" \
      --timeout-marker "${ATTEMPT_DIR}/runtime/DISPATCH_TIMEOUT.json" \
      --cleanup-result "${ATTEMPT_DIR}/runtime/CLEANUP.json" \
      --dispatch-exit-code "${code}" \
      --run-dir "${RUN_DIR}" \
      --run-id "${RUN_ID}" \
      --task-id "${TASK_ID}" >/dev/null
    reconcile_code=$?
    set -e
    if [[ "${reconcile_code}" -ne 0 || \
          "${PROCESS_CLEANUP_FAILED}" == "1" || \
          "${SUPERVISOR_CLEANUP_FAILED}" == "1" ]]; then
      KEEP_DISPATCH_LOCK_ON_EXIT=1
    else
      KEEP_DISPATCH_LOCK_ON_EXIT=0
    fi
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
: "${RDO_WORKER_ID:=}"
: "${RDO_EXECUTION_MODE:=auto}"
: "${RDO_WORKER_BACKEND:=${CONFIG_RDO_WORKER_BACKEND}}"
: "${RDO_PERMISSION_MODE:=${CONFIG_RDO_PERMISSION_MODE}}"
: "${RDO_RUNTIME_BACKEND:=${CONFIG_RDO_RUNTIME_BACKEND}}"
: "${RDO_IO_MODE:=${CONFIG_RDO_IO_MODE}}"
: "${RDO_STARTUP_TIMEOUT_SECONDS:=${CONFIG_RDO_STARTUP_TIMEOUT_SECONDS}}"
: "${RDO_TMUX_SESSION_PREFIX:=${CONFIG_RDO_TMUX_SESSION_PREFIX}}"
: "${RDO_TMUX_KEEP_SESSION:=${CONFIG_RDO_TMUX_KEEP_SESSION}}"
: "${RDO_TMUX_WAIT_TIMEOUT_SECONDS:=${CONFIG_RDO_TMUX_WAIT_TIMEOUT_SECONDS}}"
: "${RDO_FINALIZATION_GRACE_SECONDS:=90}"
: "${RDO_DEADLINE_REMINDER_SECONDS:=60}"
: "${RDO_ATTEMPT_PHASE:=auto}"

STATUS_STATE="$(python3 - "${STATUS_PATH}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("state", ""))
PY
)"
TASK_PROFILE="$(python3 - "${STATUS_PATH}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("profile", "full"))
PY
)"
TASK_ARTIFACT_PROTOCOL_VERSION="$(python3 "${PROTOCOL_CLI}" task-protocol-version \
  --task-dir "${TASK_DIR}")" || exit 2
approved_strategy_valid() {
  PYTHONPATH="${SCRIPT_DIR}" python3 - "${TASK_DIR}" >/dev/null 2>&1 <<'PY'
import sys
from pathlib import Path
from strategy import load_approved_strategy

load_approved_strategy(Path(sys.argv[1]))
PY
}
case "${TASK_PROFILE}" in
  direct|delegated|full) ;;
  *) echo "invalid task profile: ${TASK_PROFILE}" >&2; exit 2 ;;
esac
if [[ "${RDO_ATTEMPT_PHASE}" == "auto" ]]; then
  if [[ "${TASK_PROFILE}" == "full" ]]; then
    case "${STATUS_STATE}" in
      pending) RDO_ATTEMPT_PHASE="planning" ;;
      strategy_review) RDO_ATTEMPT_PHASE="execution" ;;
      blocked|changes_requested)
        if [[ -f "${TASK_DIR}/strategy/CURRENT.json" ]] && approved_strategy_valid; then
          RDO_ATTEMPT_PHASE="execution"
        else
          RDO_ATTEMPT_PHASE="planning"
        fi
        ;;
      *) echo "cannot auto-detect dispatch phase from state ${STATUS_STATE}" >&2; exit 2 ;;
    esac
  else
    case "${STATUS_STATE}" in
      pending|blocked|changes_requested) RDO_ATTEMPT_PHASE="execution" ;;
      *) echo "cannot auto-detect ${TASK_PROFILE} dispatch phase from state ${STATUS_STATE}" >&2; exit 2 ;;
    esac
  fi
fi
case "${RDO_ATTEMPT_PHASE}" in
  planning|execution) ;;
  *) echo "RDO_ATTEMPT_PHASE must be planning or execution" >&2; exit 2 ;;
esac
if [[ "${TASK_PROFILE}" != "full" && "${RDO_ATTEMPT_PHASE}" != "execution" ]]; then
  echo "task profile ${TASK_PROFILE} does not use planning attempts" >&2
  exit 2
fi

# V2 readiness is deliberately checked before creating a dispatch lock,
# attempt directory, FSM transition, branch, or worktree.  The same validation
# is repeated under the lock when TASK_INPUTS.json is frozen.
if [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" == "2" ]]; then
  python3 "${PROTOCOL_CLI}" check-task-readiness \
    --task-dir "${TASK_DIR}" \
    --run-dir "${RUN_DIR}" \
    --task-id "${TASK_ID}" \
    --profile "${TASK_PROFILE}" >/dev/null
elif [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" != "1" ]]; then
  echo "unsupported artifact protocol version: ${TASK_ARTIFACT_PROTOCOL_VERSION}" >&2
  exit 2
fi

STRATEGY_ID=""
STRATEGY_SHA256=""
STRATEGY_REVISION=""
STRATEGY_PATH=""
if [[ "${RDO_ATTEMPT_PHASE}" == "execution" && "${TASK_PROFILE}" == "full" ]]; then
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

if [[ "${RDO_RUNTIME_BACKEND}:${RDO_IO_MODE}" != "plain:machine" && \
      "${RDO_RUNTIME_BACKEND}:${RDO_IO_MODE}" != "tmux:human" ]]; then
  echo "unsupported runtime/io combination: ${RDO_RUNTIME_BACKEND} + ${RDO_IO_MODE}" >&2
  echo "supported combinations: plain + machine; tmux + human" >&2
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
if ! [[ "${RDO_STARTUP_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "RDO_STARTUP_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi
if ! python3 - "${RDO_FINALIZATION_GRACE_SECONDS}" "${RDO_DEADLINE_REMINDER_SECONDS}" <<'PY'
import math
import sys

try:
    values = [float(item) for item in sys.argv[1:]]
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if all(math.isfinite(item) and item > 0 for item in values) else 1)
PY
then
  echo "RDO finalization grace and deadline reminder must be positive finite seconds" >&2
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

if [[ -n "${RDO_WORKER_COMMAND}" && "${RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE:-0}" != "1" ]]; then
  echo "worker.command overrides do not provide a registered startup-event contract; use the registered backend command" >&2
  exit 2
fi

ATTEMPT_SEQ="$(find "${TASK_DIR}/attempts" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
ATTEMPT_NUM="$(printf "%03d" "$((ATTEMPT_SEQ + 1))")"
ATTEMPT_BACKEND_SHORT="$(printf "%s" "${RDO_WORKER_BACKEND}" | sed 's/-code$//' | sanitize_name | cut -c1-12)"
ATTEMPT_ID="A${ATTEMPT_NUM}-${ATTEMPT_BACKEND_SHORT:-worker}-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(3))
PY
)"
ATTEMPT_POLICY_TIMEOUT_SECONDS="${ATTEMPT_TIMEOUT_SECONDS}"
set +e
TASK_BUDGET_ASSESSMENT="$(python3 "${TASK_BUDGET_CLI}" assess \
  --task-dir "${TASK_DIR}" \
  --artifact-protocol-version "${TASK_ARTIFACT_PROTOCOL_VERSION}" \
  --attempt-wall-seconds "${ATTEMPT_POLICY_TIMEOUT_SECONDS}" \
  --next-attempt-id "${ATTEMPT_ID}")"
TASK_BUDGET_CODE=$?
set -e
if [[ "${TASK_BUDGET_CODE}" -ne 0 ]]; then
  if [[ -n "${TASK_BUDGET_ASSESSMENT}" ]]; then
    printf '%s\n' "${TASK_BUDGET_ASSESSMENT}" >&2
  fi
  exit "${TASK_BUDGET_CODE}"
fi
TASK_BUDGET_ENABLED="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1]).get("enabled") else "0")' "${TASK_BUDGET_ASSESSMENT}")"
ATTEMPT_TIMEOUT_SECONDS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["admission"]["attempt_wall_seconds"] or sys.argv[2])' "${TASK_BUDGET_ASSESSMENT}" "${ATTEMPT_POLICY_TIMEOUT_SECONDS}")"

BACKEND_PROFILE_COMPILE_ARGS=(
  compile
  --repo-root "${REPO_ROOT}"
  --task-dir "${TASK_DIR}"
  --backend "${RDO_WORKER_BACKEND}"
  --phase "${RDO_ATTEMPT_PHASE}"
  --io-mode "${RDO_IO_MODE}"
)
if [[ -n "${STRATEGY_PATH}" ]]; then
  BACKEND_PROFILE_COMPILE_ARGS+=(--strategy "${STRATEGY_PATH}")
fi
if [[ "${TASK_BUDGET_ENABLED}" == "1" ]]; then
  BACKEND_PROFILE_COMPILE_ARGS+=(--task-budget-json "${TASK_BUDGET_ASSESSMENT}")
fi
BACKEND_PROFILE_JSON="$(python3 "${BACKEND_GOVERNANCE_CLI}" "${BACKEND_PROFILE_COMPILE_ARGS[@]}")" || exit 2

WORKER_CONTEXT="$(python3 - "${STATUS_PATH}" "${RDO_WORKER_BACKEND}" <<'PY'
import json, sys
status = json.load(open(sys.argv[1], encoding="utf-8"))
backend = sys.argv[2]
assigned = status.get("assigned_worker") or {}
same_backend = assigned.get("backend_id") == backend
print(json.dumps({
    "parent_attempt_id": status.get("current_attempt_id") or "",
    "worker_id": assigned.get("worker_id", "") if same_backend else "",
    "session_id": (assigned.get("backend_session_id") or assigned.get("session_id") or "") if same_backend else "",
    "had_worker": bool(assigned),
    "same_backend": same_backend,
}))
PY
)"
PARENT_ATTEMPT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["parent_attempt_id"])' "${WORKER_CONTEXT}")"
if [[ -z "${RDO_WORKER_ID}" ]]; then
  RDO_WORKER_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["worker_id"])' "${WORKER_CONTEXT}")"
fi
if [[ -z "${RDO_WORKER_ID}" ]]; then
  RDO_WORKER_ID="W-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(4))
PY
)"
fi
if [[ -z "${RDO_BACKEND_SESSION_ID}" ]]; then
  RDO_BACKEND_SESSION_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["session_id"])' "${WORKER_CONTEXT}")"
fi
if [[ "${RDO_EXECUTION_MODE}" == "auto" ]]; then
  HAD_WORKER="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1])["had_worker"] else "0")' "${WORKER_CONTEXT}")"
  SAME_BACKEND="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1])["same_backend"] else "0")' "${WORKER_CONTEXT}")"
  if [[ "${SAME_BACKEND}" == "1" && -n "${RDO_BACKEND_SESSION_ID}" ]]; then
    RDO_EXECUTION_MODE="resume"
  elif [[ "${HAD_WORKER}" == "1" && "${SAME_BACKEND}" != "1" ]]; then
    RDO_EXECUTION_MODE="replace"
  else
    RDO_EXECUTION_MODE="start"
  fi
fi
case "${RDO_EXECUTION_MODE}" in
  start|resume|replace) ;;
  *) echo "RDO_EXECUTION_MODE must be start, resume, or replace" >&2; exit 2 ;;
esac
if [[ "${RDO_EXECUTION_MODE}" == "resume" && -z "${RDO_BACKEND_SESSION_ID}" ]]; then
  echo "resume requires a backend session id" >&2
  exit 2
fi
REQUESTED_EXECUTION_MODE="${RDO_EXECUTION_MODE}"
REQUESTED_SESSION_ID="${RDO_BACKEND_SESSION_ID}"
RESUME_FALLBACK_REASON=""
PREFLIGHT_JSON="$(python3 "${BACKEND_PREFLIGHT}" \
  --backend "${RDO_WORKER_BACKEND}" \
  --command "${RDO_WORKER_COMMAND}" \
  --execution-mode "${REQUESTED_EXECUTION_MODE}" \
  --session-id "${REQUESTED_SESSION_ID}" \
  --cwd "${REPO_ROOT}" \
  --io-mode "${RDO_IO_MODE}")" || exit 2
RESUME_FALLBACK_REQUIRED="$(python3 -c '
import json, sys
print("1" if json.loads(sys.argv[1]).get("resume", {}).get("fallback_required") else "0")
' "${PREFLIGHT_JSON}")"
if [[ "${RESUME_FALLBACK_REQUIRED}" == "1" ]]; then
  RESUME_FALLBACK_REASON="$(python3 -c '
import json, sys
print(json.loads(sys.argv[1]).get("resume", {}).get("fallback_reason") or "resume_unavailable")
' "${PREFLIGHT_JSON}")"
  RDO_EXECUTION_MODE="start"
  if [[ "${RDO_WORKER_BACKEND}" != "claude-code" ]]; then
    RDO_BACKEND_SESSION_ID=""
  fi
fi
if [[ "${RDO_EXECUTION_MODE}" != "resume" && "${RDO_WORKER_BACKEND}" == "claude-code" && -z "${RDO_BACKEND_SESSION_ID}" ]]; then
  RDO_BACKEND_SESSION_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
fi
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

set +e
TASK_BUDGET_RECHECK="$(python3 "${TASK_BUDGET_CLI}" assess \
  --task-dir "${TASK_DIR}" \
  --artifact-protocol-version "${TASK_ARTIFACT_PROTOCOL_VERSION}" \
  --attempt-wall-seconds "${ATTEMPT_POLICY_TIMEOUT_SECONDS}" \
  --next-attempt-id "${ATTEMPT_ID}")"
TASK_BUDGET_CODE=$?
set -e
if [[ "${TASK_BUDGET_CODE}" -ne 0 ]]; then
  if [[ -n "${TASK_BUDGET_RECHECK}" ]]; then
    printf '%s\n' "${TASK_BUDGET_RECHECK}" >&2
  fi
  exit "${TASK_BUDGET_CODE}"
fi
if ! python3 -c 'import json,sys; raise SystemExit(0 if json.loads(sys.argv[1]) == json.loads(sys.argv[2]) else 1)' \
  "${TASK_BUDGET_ASSESSMENT}" "${TASK_BUDGET_RECHECK}"; then
  echo "task budget history changed during dispatch; retry admission" >&2
  exit 3
fi

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

TASK_INPUTS_SHA256=""
TASK_INPUTS_REF=""
TASK_BASE_COMMIT=""
WORKTREE_BEFORE_SHA256=""
TASK_BUDGET_REF=""
TASK_BUDGET_SHA256=""
if [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" == "2" ]]; then
  TASK_INPUTS_RESULT="$(python3 "${PROTOCOL_CLI}" freeze-task-inputs \
    --task-dir "${TASK_DIR}" \
    --run-dir "${RUN_DIR}" \
    --repo-root "${REPO_ROOT}" \
    --task-id "${TASK_ID}" \
    --attempt-id "${ATTEMPT_ID}" \
    --attempt-dir "${ATTEMPT_DIR}" \
    --profile "${TASK_PROFILE}" \
    --execution-mode "${RDO_EXECUTION_MODE}")" || exit 2
  TASK_INPUTS_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha256"])' "${TASK_INPUTS_RESULT}")"
  TASK_INPUTS_REF="TASK_INPUTS.json"
  TASK_BASE_COMMIT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["task_base_commit"])' "${TASK_INPUTS_RESULT}")"
else
  mkdir -p "${ATTEMPT_DIR}"
fi

if [[ "${TASK_BUDGET_ENABLED}" == "1" ]]; then
  TASK_BUDGET_RESULT="$(python3 "${TASK_BUDGET_CLI}" freeze \
    --attempt-dir "${ATTEMPT_DIR}" \
    --assessment-json "${TASK_BUDGET_ASSESSMENT}")" || exit 2
  TASK_BUDGET_REF="runtime/TASK_BUDGET.json"
  TASK_BUDGET_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sha256"])' "${TASK_BUDGET_RESULT}")"
fi

BACKEND_MATERIALIZED_JSON="$(python3 "${BACKEND_GOVERNANCE_CLI}" materialize \
  --profile-json "${BACKEND_PROFILE_JSON}" \
  --runtime-dir "${ATTEMPT_DIR}/runtime")" || exit 2
BACKEND_PROFILE_PATH="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["profile_path"])' "${BACKEND_MATERIALIZED_JSON}")"
BACKEND_PROFILE_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["profile_sha256"])' "${BACKEND_MATERIALIZED_JSON}")"
BACKEND_SETTINGS_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("settings_sha256") or "")' "${BACKEND_MATERIALIZED_JSON}")"
READ_POLICY_SHA256="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("read_policy_sha256") or "")' "${BACKEND_MATERIALIZED_JSON}")"
printf '%s\n' "${PREFLIGHT_JSON}" > "${ATTEMPT_DIR}/runtime/PREFLIGHT.json"

if [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" == "1" ]]; then
  for artifact in EVIDENCE.md HANDOFF.md HANDOFF.json; do
    if [[ -f "${TASK_DIR}/${artifact}" ]]; then
      cp "${TASK_DIR}/${artifact}" "${ATTEMPT_DIR}/preexisting-${artifact}"
    fi
    cp "${SKILL_ROOT}/templates/task/${artifact}" "${TASK_DIR}/${artifact}"
  done
  rm -f "${ATTEMPT_DIR}/COMPLETION.json" "${ATTEMPT_DIR}/COMPLETION.json.tmp"
fi

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
      if [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" == "2" ]]; then
        git -C "${REPO_ROOT}" worktree add -b "${BRANCH}" "${WORKTREE_PATH}" "${TASK_BASE_COMMIT}"
      else
        git -C "${REPO_ROOT}" worktree add -b "${BRANCH}" "${WORKTREE_PATH}" HEAD
      fi
    fi
  fi
fi

render_dispatch_prompt() {
  local prompt_mode="$1"
  local prompt_mode_reason="$2"
  python3 "${DISPATCH_ASSETS}" render-prompt \
    --output "${ATTEMPT_DIR}/prompt.md" \
    --worktree-path "${WORKTREE_PATH}" \
    --task-dir "${TASK_DIR}" \
    --status-path "${STATUS_PATH}" \
    --attempt-dir "${ATTEMPT_DIR}" \
    --worker-backend "${RDO_WORKER_BACKEND}" \
    --agent-name "${RDO_WORKER_AGENT_NAME}" \
    --phase "${RDO_ATTEMPT_PHASE}" \
    --strategy-path "${STRATEGY_PATH}" \
    --prompt-mode "${prompt_mode}" \
    --prompt-mode-reason "${prompt_mode_reason}"
}

if [[ "${RDO_EXECUTION_MODE}" == "resume" ]]; then
  render_dispatch_prompt "compact_resume" "backend_session_resume_preflight_passed"
elif [[ -n "${RESUME_FALLBACK_REASON}" ]]; then
  render_dispatch_prompt "full" "preflight_resume_fallback:${RESUME_FALLBACK_REASON}"
elif [[ "${RDO_EXECUTION_MODE}" == "replace" ]]; then
  render_dispatch_prompt "full" "backend_replacement_session"
else
  render_dispatch_prompt "full" "new_backend_session"
fi

PROMPT_TRANSPORT="stdin"
PROMPT_SUBMIT_KEY=""
PROMPT_POST_PASTE_DELAY_MS="0"
BACKEND_ARGV_JSON="[]"
BACKEND_ENVIRONMENT_JSON="{}"
REGISTERED_BACKEND_COMMAND=0
if [[ -z "${RDO_WORKER_COMMAND}" ]]; then
  REGISTERED_BACKEND_COMMAND=1
  BACKEND_COMMAND_JSON="$(python3 "${AGENT_BACKEND_CLI}" command \
    --backend "${RDO_WORKER_BACKEND}" \
    --io-mode "${RDO_IO_MODE}" \
    --permission-mode "${RDO_PERMISSION_MODE}" \
    --cwd "${WORKTREE_PATH}" \
    --prompt-path "${ATTEMPT_DIR}/prompt.md" \
    --agent-name "${RDO_WORKER_AGENT_NAME}" \
    --execution-mode "${RDO_EXECUTION_MODE}" \
    --session-id "${RDO_BACKEND_SESSION_ID}" \
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
  BACKEND_ARGV_JSON="$(python3 - <<'PY' "${BACKEND_COMMAND_JSON}"
import json, sys
print(json.dumps(json.loads(sys.argv[1])["argv"], separators=(",", ":")))
PY
)"
  BACKEND_ENVIRONMENT_JSON="$(python3 - <<'PY' "${BACKEND_COMMAND_JSON}"
import json, sys
print(json.dumps(json.loads(sys.argv[1])["environment"], separators=(",", ":")))
PY
)"
fi

ORIGINAL_WORKER_COMMAND="${RDO_WORKER_COMMAND}"
SUPERVISOR_RESULT="${ATTEMPT_DIR}/supervisor-result.json"
DEADLINE_PATH="${ATTEMPT_DIR}/runtime/DEADLINE.json"
deadline_allows_resume_fallback() {
  python3 - "${DEADLINE_PATH}" <<'PY'
import json
import sys
import time

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    deadline = float(payload["execution_deadline_at_epoch"])
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if time.time() < deadline else 1)
PY
}
supervisor_allows_resume_fallback() {
  python3 - "${SUPERVISOR_RESULT}" "${DEADLINE_PATH}" "${RDO_RUNTIME_BACKEND}" "${EXIT_CODE:-}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

try:
    result_path = Path(sys.argv[1])
    deadline_path = Path(sys.argv[2])
    if result_path.is_symlink() or deadline_path.is_symlink():
        raise ValueError("unsafe supervisor/deadline path")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    deadline_raw = deadline_path.read_bytes()
    deadline = json.loads(deadline_raw)
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
survivors = payload.get("surviving_pids")
clean = (
    payload.get("cleanup_verified") is True
    and isinstance(survivors, list)
    and survivors == []
    and payload.get("timed_out") is False
    and payload.get("publication_requested") is False
    and isinstance(payload.get("deadline_sha256"), str)
    and hashlib.sha256(deadline_raw).hexdigest()
    == payload.get("deadline_sha256")
    and payload.get("execution_deadline_at_epoch")
    == deadline.get("execution_deadline_at_epoch")
)
if sys.argv[3] == "plain":
    clean = (
        clean
        and type(payload.get("exit_code")) is int
        and payload.get("exit_code") == 125
        and payload.get("startup_failed") is True
    )
else:
    try:
        expected_exit = int(sys.argv[4])
    except ValueError:
        clean = False
    else:
        clean = (
            clean
            and expected_exit != 0
            and type(payload.get("exit_code")) is int
            and payload.get("exit_code") == expected_exit
        )
raise SystemExit(0 if clean else 1)
PY
}
if [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" == "2" ]]; then
  HANDOFF_PUBLICATION_PATH="${ATTEMPT_DIR}/runtime/HANDOFF_READY.json"
  TRANSCRIPT_PATH="${ATTEMPT_DIR}/runtime/transcript.log"
else
  HANDOFF_PUBLICATION_PATH="${ATTEMPT_DIR}/COMPLETION.json"
  TRANSCRIPT_PATH="${ATTEMPT_DIR}/transcript.log"
fi
if [[ "${RDO_RUNTIME_BACKEND}" == "plain" ]]; then
  if [[ "${REGISTERED_BACKEND_COMMAND}" == "0" ]]; then
    BACKEND_ARGV_JSON="$(python3 -c 'import json,sys; print(json.dumps(["/bin/bash", "-c", sys.argv[1]], separators=(",", ":")))' "${ORIGINAL_WORKER_COMMAND}")"
  fi
  RDO_WORKER_COMMAND="$(python3 - \
    "${SCRIPT_DIR}/machine_attempt_supervisor.py" "${RDO_WORKER_BACKEND}" \
    "${BACKEND_ARGV_JSON}" "${BACKEND_ENVIRONMENT_JSON}" "${WORKTREE_PATH}" \
    "${ATTEMPT_DIR}/prompt.md" "${PROMPT_TRANSPORT}" "${RDO_STARTUP_TIMEOUT_SECONDS}" \
    "${ATTEMPT_TIMEOUT_SECONDS}" "${ATTEMPT_DIR}/runtime/STARTUP.json" \
    "${SUPERVISOR_RESULT}" "${ATTEMPT_DIR}/runtime/supervisor.json" \
    "${TRANSCRIPT_PATH}" "${STRATEGY_ID}" "${STRATEGY_SHA256}" \
    "${ATTEMPT_DIR}/runtime/SESSION.json" "${RDO_BACKEND_SESSION_ID}" \
    "${REGISTERED_BACKEND_COMMAND}" "${BACKEND_PROFILE_PATH}" \
    "${TASK_ARTIFACT_PROTOCOL_VERSION}" "${HANDOFF_PUBLICATION_PATH}" \
    "${TASK_DIR}" "${ATTEMPT_ID}" "${ATTEMPT_DIR}/runtime/FINALIZATION.json" \
    "${DEADLINE_PATH}" "${RDO_FINALIZATION_GRACE_SECONDS}" \
    "${RDO_DEADLINE_REMINDER_SECONDS}" <<'PY'
import shlex, sys
(
    script, backend, argv_json, environment_json, cwd, prompt_path,
    prompt_transport, startup_timeout, timeout, startup_result,
    supervisor_result, supervisor_state, transcript, strategy_id,
    strategy_sha256, session_result, existing_session_id, registered, backend_profile,
    artifact_protocol_version, publication_path, task_dir, attempt_id,
    finalization_path, deadline_path, finalization_grace, deadline_reminder,
) = sys.argv[1:]
parts = [
    sys.executable, script, "--backend", backend,
    "--argv-json", argv_json, "--environment-json", environment_json,
    "--cwd", cwd, "--prompt-path", prompt_path,
    "--prompt-transport", prompt_transport,
    "--startup-timeout-seconds", startup_timeout,
    "--timeout-seconds", timeout, "--startup-result", startup_result,
    "--supervisor-result", supervisor_result, "--supervisor-state", supervisor_state,
    "--transcript", transcript, "--strategy-id", strategy_id,
    "--strategy-sha256", strategy_sha256,
    "--session-result", session_result,
    "--existing-session-id", existing_session_id,
    "--backend-profile", backend_profile,
    "--artifact-protocol-version", artifact_protocol_version,
    "--publication-path", publication_path,
    "--task-dir", task_dir,
    "--attempt-id", attempt_id,
    "--finalization-path", finalization_path,
    "--finalization-timeout-seconds", finalization_grace,
    "--deadline-path", deadline_path,
    "--deadline-reminder-seconds", deadline_reminder,
]
if registered == "0":
    parts.append("--custom-command")
print(" ".join(shlex.quote(part) for part in parts))
PY
)"
else
  RDO_WORKER_COMMAND="$(python3 - "${SCRIPT_DIR}/supervise_attempt.py" "${ATTEMPT_TIMEOUT_SECONDS}" "${SUPERVISOR_RESULT}" "${WORKTREE_PATH}" "${ORIGINAL_WORKER_COMMAND}" "${STRATEGY_ID}" "${STRATEGY_SHA256}" "${TASK_ARTIFACT_PROTOCOL_VERSION}" "${HANDOFF_PUBLICATION_PATH}" "${TASK_DIR}" "${ATTEMPT_ID}" "${ATTEMPT_DIR}/runtime/FINALIZATION.json" "${DEADLINE_PATH}" "${RDO_FINALIZATION_GRACE_SECONDS}" "${RDO_DEADLINE_REMINDER_SECONDS}" <<'PY'
import shlex, sys
script, timeout, result, cwd, command, strategy_id, strategy_sha256, artifact_protocol_version, publication_path, task_dir, attempt_id, finalization_path, deadline_path, finalization_grace, deadline_reminder = sys.argv[1:]
print(" ".join([
    shlex.quote(sys.executable), shlex.quote(script),
    "--timeout-seconds", shlex.quote(timeout),
    "--result", shlex.quote(result),
    "--cwd", shlex.quote(cwd),
    "--strategy-id", shlex.quote(strategy_id),
    "--strategy-sha256", shlex.quote(strategy_sha256),
    "--artifact-protocol-version", shlex.quote(artifact_protocol_version),
    "--publication-path", shlex.quote(publication_path),
    "--task-dir", shlex.quote(task_dir),
    "--attempt-id", shlex.quote(attempt_id),
    "--finalization-path", shlex.quote(finalization_path),
    "--finalization-timeout-seconds", shlex.quote(finalization_grace),
    "--deadline-path", shlex.quote(deadline_path),
    "--deadline-reminder-seconds", shlex.quote(deadline_reminder),
    "--shell-command", shlex.quote(command),
]))
PY
)"
fi

python3 "${PROTOCOL_CLI}" create-attempt \
  --path "${ATTEMPT_DIR}/ATTEMPT.json" \
  --artifact-protocol-version "${TASK_ARTIFACT_PROTOCOL_VERSION}" \
  --task-inputs-ref "${TASK_INPUTS_REF}" \
  --task-inputs-sha256 "${TASK_INPUTS_SHA256}" \
  --task-budget-ref "${TASK_BUDGET_REF}" \
  --task-budget-sha256 "${TASK_BUDGET_SHA256}" \
  --attempt-id "${ATTEMPT_ID}" \
  --task-id "${TASK_ID}" \
  --agent-name "${RDO_WORKER_AGENT_NAME}" \
  --worker-id "${RDO_WORKER_ID}" \
  --parent-attempt-id "${PARENT_ATTEMPT_ID}" \
  --session-id "${RDO_BACKEND_SESSION_ID}" \
  --worker-backend "${RDO_WORKER_BACKEND}" \
  --execution-mode "${RDO_EXECUTION_MODE}" \
  --requested-execution-mode "${REQUESTED_EXECUTION_MODE}" \
  --requested-session-id "${REQUESTED_SESSION_ID}" \
  --resume-fallback-reason "${RESUME_FALLBACK_REASON}" \
  --resume-reason "${STATUS_STATE}" \
  --phase "${RDO_ATTEMPT_PHASE}" \
  --strategy-id "${STRATEGY_ID}" \
  --strategy-revision "${STRATEGY_REVISION}" \
  --strategy-sha256 "${STRATEGY_SHA256}" \
  --backend-profile-sha256 "${BACKEND_PROFILE_SHA256}" \
  --backend-settings-sha256 "${BACKEND_SETTINGS_SHA256}" \
  --read-policy-sha256 "${READ_POLICY_SHA256}" \
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
  --worker-id "${RDO_WORKER_ID}" \
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
  WORKTREE_BEFORE_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "${ATTEMPT_DIR}/runtime/worktree-before.json")"
  if [[ "${TASK_PROFILE}" == "full" && "${RDO_ATTEMPT_PHASE}" == "execution" ]]; then
    python3 "${RESUME_CONTEXT_CLI}" \
      --task-dir "${TASK_DIR}" \
      --attempt-dir "${ATTEMPT_DIR}" \
      --strategy "${STRATEGY_PATH}" \
      --current-worktree-before "${ATTEMPT_DIR}/runtime/worktree-before.json" \
      > /dev/null
  fi
fi

if [[ "${DISPATCH_DRY_RUN}" == "1" ]]; then
  echo "dry run: prompt written to ${ATTEMPT_DIR}/prompt.md" | tee "${ATTEMPT_DIR}/result.md"
  touch "${TRANSCRIPT_PATH}"
  EXIT_CODE=0
  EXIT_CODE_RAW="0"
else
  if [[ "${RDO_RUNTIME_BACKEND}" == "plain" ]]; then
    set +e
    (cd "${WORKTREE_PATH}" && eval "${RDO_WORKER_COMMAND}" < /dev/null)
    EXIT_CODE=$?
    set -e
    EXIT_CODE_RAW="${EXIT_CODE}"
    if [[ "${REGISTERED_BACKEND_COMMAND}" == "1" && \
          "${REQUESTED_EXECUTION_MODE}" == "resume" && \
          "${RDO_EXECUTION_MODE}" == "resume" && \
          "${EXIT_CODE}" -eq 125 && \
          -f "${ATTEMPT_DIR}/runtime/STARTUP.json" ]]; then
      RUNTIME_FALLBACK="$(python3 - "${ATTEMPT_DIR}/runtime/STARTUP.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
failure = payload.get("failure") if isinstance(payload, dict) else None
print("1" if isinstance(failure, dict) and failure.get("recoverable_resume_failure") is True else "0")
PY
)"
      if [[ "${RUNTIME_FALLBACK}" == "1" ]] && \
         [[ "${EXIT_CODE}" -eq 125 ]] && \
         deadline_allows_resume_fallback && \
         supervisor_allows_resume_fallback; then
        cp "${ATTEMPT_DIR}/runtime/STARTUP.json" \
          "${ATTEMPT_DIR}/runtime/RESUME_STARTUP_FAILURE.json"
        [[ ! -f "${SUPERVISOR_RESULT}" ]] || cp "${SUPERVISOR_RESULT}" \
          "${ATTEMPT_DIR}/runtime/RESUME_SUPERVISOR_FAILURE.json"
        [[ ! -f "${TRANSCRIPT_PATH}" ]] || cp "${TRANSCRIPT_PATH}" \
          "${ATTEMPT_DIR}/runtime/resume-failure-transcript.log"
        FALLBACK_SESSION_ID=""
        if [[ "${RDO_WORKER_BACKEND}" == "claude-code" ]]; then
          FALLBACK_SESSION_ID="${REQUESTED_SESSION_ID}"
        fi
        render_dispatch_prompt "full" "runtime_resume_fallback:runtime_session_not_found"
        FALLBACK_COMMAND_JSON="$(python3 "${AGENT_BACKEND_CLI}" command \
          --backend "${RDO_WORKER_BACKEND}" \
          --io-mode "${RDO_IO_MODE}" \
          --permission-mode "${RDO_PERMISSION_MODE}" \
          --cwd "${WORKTREE_PATH}" \
          --prompt-path "${ATTEMPT_DIR}/prompt.md" \
          --agent-name "${RDO_WORKER_AGENT_NAME}" \
          --execution-mode start \
          --session-id "${FALLBACK_SESSION_ID}" \
          --backend-profile "${BACKEND_PROFILE_PATH}" \
          --json)"
        FALLBACK_ORIGINAL_COMMAND="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["command"])' "${FALLBACK_COMMAND_JSON}")"
        FALLBACK_ARGV_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["argv"], separators=(",", ":")))' "${FALLBACK_COMMAND_JSON}")"
        FALLBACK_ENVIRONMENT_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["environment"], separators=(",", ":")))' "${FALLBACK_COMMAND_JSON}")"
        FALLBACK_PROMPT_TRANSPORT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["prompt_transport"])' "${FALLBACK_COMMAND_JSON}")"
        FALLBACK_WORKER_COMMAND="$(python3 - \
          "${SCRIPT_DIR}/machine_attempt_supervisor.py" "${RDO_WORKER_BACKEND}" \
          "${FALLBACK_ARGV_JSON}" "${FALLBACK_ENVIRONMENT_JSON}" "${WORKTREE_PATH}" \
          "${ATTEMPT_DIR}/prompt.md" "${FALLBACK_PROMPT_TRANSPORT}" "${RDO_STARTUP_TIMEOUT_SECONDS}" \
          "${ATTEMPT_TIMEOUT_SECONDS}" "${ATTEMPT_DIR}/runtime/STARTUP.json" \
          "${SUPERVISOR_RESULT}" "${ATTEMPT_DIR}/runtime/supervisor.json" \
          "${TRANSCRIPT_PATH}" "${STRATEGY_ID}" "${STRATEGY_SHA256}" \
          "${ATTEMPT_DIR}/runtime/SESSION.json" "${FALLBACK_SESSION_ID}" \
          "${BACKEND_PROFILE_PATH}" "${TASK_ARTIFACT_PROTOCOL_VERSION}" \
          "${HANDOFF_PUBLICATION_PATH}" "${TASK_DIR}" "${ATTEMPT_ID}" \
          "${ATTEMPT_DIR}/runtime/FINALIZATION.json" "${DEADLINE_PATH}" \
          "${RDO_FINALIZATION_GRACE_SECONDS}" \
          "${RDO_DEADLINE_REMINDER_SECONDS}" <<'PY'
import shlex
import sys

(
    script, backend, argv_json, environment_json, cwd, prompt_path,
    prompt_transport, startup_timeout, timeout, startup_result,
    supervisor_result, supervisor_state, transcript, strategy_id,
    strategy_sha256, session_result, existing_session_id, backend_profile,
    artifact_protocol_version, publication_path, task_dir, attempt_id,
    finalization_path, deadline_path, finalization_grace, deadline_reminder,
) = sys.argv[1:]
parts = [
    sys.executable, script, "--backend", backend,
    "--argv-json", argv_json, "--environment-json", environment_json,
    "--cwd", cwd, "--prompt-path", prompt_path,
    "--prompt-transport", prompt_transport,
    "--startup-timeout-seconds", startup_timeout,
    "--timeout-seconds", timeout, "--startup-result", startup_result,
    "--supervisor-result", supervisor_result, "--supervisor-state", supervisor_state,
    "--transcript", transcript, "--strategy-id", strategy_id,
    "--strategy-sha256", strategy_sha256,
    "--session-result", session_result,
    "--existing-session-id", existing_session_id,
    "--backend-profile", backend_profile,
    "--artifact-protocol-version", artifact_protocol_version,
    "--publication-path", publication_path,
    "--task-dir", task_dir,
    "--attempt-id", attempt_id,
    "--finalization-path", finalization_path,
    "--finalization-timeout-seconds", finalization_grace,
    "--deadline-path", deadline_path,
    "--deadline-reminder-seconds", deadline_reminder,
]
print(" ".join(shlex.quote(part) for part in parts))
PY
)"
        python3 "${PROTOCOL_CLI}" record-resume-fallback \
          --status-path "${STATUS_PATH}" \
          --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json" \
          --failure-path "${ATTEMPT_DIR}/runtime/RESUME_STARTUP_FAILURE.json" \
          --requested-session-id "${REQUESTED_SESSION_ID}" \
          --fallback-session-id "${FALLBACK_SESSION_ID}" \
          --reason "runtime_session_not_found" \
          --source runtime \
          --command "${FALLBACK_ORIGINAL_COMMAND}" \
          --supervisor-command "${FALLBACK_WORKER_COMMAND}"
        rm -f "${ATTEMPT_DIR}/runtime/STARTUP.json" \
          "${ATTEMPT_DIR}/runtime/SESSION.json" \
          "${SUPERVISOR_RESULT}"
        RDO_EXECUTION_MODE="start"
        RDO_BACKEND_SESSION_ID="${FALLBACK_SESSION_ID}"
        RDO_WORKER_COMMAND="${FALLBACK_WORKER_COMMAND}"
        set +e
        (cd "${WORKTREE_PATH}" && eval "${RDO_WORKER_COMMAND}" < /dev/null)
        EXIT_CODE=$?
        set -e
        EXIT_CODE_RAW="${EXIT_CODE}"
      fi
    fi
  else
    run_tmux_worker_once
    if [[ "${REGISTERED_BACKEND_COMMAND}" == "1" && \
          "${REQUESTED_EXECUTION_MODE}" == "resume" && \
          "${RDO_EXECUTION_MODE}" == "resume" && \
          -f "${ATTEMPT_DIR}/runtime/STARTUP.json" ]]; then
      RUNTIME_FALLBACK="$(python3 - "${ATTEMPT_DIR}/runtime/STARTUP.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
failure = payload.get("failure") if isinstance(payload, dict) else None
print("1" if isinstance(failure, dict) and failure.get("recoverable_resume_failure") is True else "0")
PY
)"
      if [[ "${RUNTIME_FALLBACK}" == "1" ]] && \
         deadline_allows_resume_fallback && \
         supervisor_allows_resume_fallback; then
        cp "${ATTEMPT_DIR}/runtime/STARTUP.json" \
          "${ATTEMPT_DIR}/runtime/RESUME_STARTUP_FAILURE.json"
        [[ ! -f "${SUPERVISOR_RESULT}" ]] || cp "${SUPERVISOR_RESULT}" \
          "${ATTEMPT_DIR}/runtime/RESUME_SUPERVISOR_FAILURE.json"
        [[ ! -f "${TRANSCRIPT_PATH}" ]] || cp "${TRANSCRIPT_PATH}" \
          "${ATTEMPT_DIR}/runtime/resume-failure-transcript.log"
        tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
        FALLBACK_SESSION_ID=""
        if [[ "${RDO_WORKER_BACKEND}" == "claude-code" ]]; then
          FALLBACK_SESSION_ID="${REQUESTED_SESSION_ID}"
        fi
        render_dispatch_prompt "full" "runtime_resume_fallback:runtime_session_not_found"
        FALLBACK_COMMAND_JSON="$(python3 "${AGENT_BACKEND_CLI}" command \
          --backend "${RDO_WORKER_BACKEND}" \
          --io-mode "${RDO_IO_MODE}" \
          --permission-mode "${RDO_PERMISSION_MODE}" \
          --cwd "${WORKTREE_PATH}" \
          --prompt-path "${ATTEMPT_DIR}/prompt.md" \
          --agent-name "${RDO_WORKER_AGENT_NAME}" \
          --execution-mode start \
          --session-id "${FALLBACK_SESSION_ID}" \
          --backend-profile "${BACKEND_PROFILE_PATH}" \
          --json)"
        FALLBACK_ORIGINAL_COMMAND="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["command"])' "${FALLBACK_COMMAND_JSON}")"
        PROMPT_TRANSPORT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["prompt_transport"])' "${FALLBACK_COMMAND_JSON}")"
        PROMPT_SUBMIT_KEY="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("submit_key") or "")' "${FALLBACK_COMMAND_JSON}")"
        PROMPT_POST_PASTE_DELAY_MS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("post_paste_delay_ms") or 0)' "${FALLBACK_COMMAND_JSON}")"
        FALLBACK_WORKER_COMMAND="$(python3 - \
          "${SCRIPT_DIR}/supervise_attempt.py" "${ATTEMPT_TIMEOUT_SECONDS}" \
          "${SUPERVISOR_RESULT}" "${WORKTREE_PATH}" "${FALLBACK_ORIGINAL_COMMAND}" \
          "${STRATEGY_ID}" "${STRATEGY_SHA256}" "${TASK_ARTIFACT_PROTOCOL_VERSION}" \
          "${HANDOFF_PUBLICATION_PATH}" "${TASK_DIR}" "${ATTEMPT_ID}" \
          "${ATTEMPT_DIR}/runtime/FINALIZATION.json" "${DEADLINE_PATH}" \
          "${RDO_FINALIZATION_GRACE_SECONDS}" \
          "${RDO_DEADLINE_REMINDER_SECONDS}" <<'PY'
import shlex
import sys

script, timeout, result, cwd, command, strategy_id, strategy_sha256, artifact_protocol_version, publication_path, task_dir, attempt_id, finalization_path, deadline_path, finalization_grace, deadline_reminder = sys.argv[1:]
print(" ".join([
    shlex.quote(sys.executable), shlex.quote(script),
    "--timeout-seconds", shlex.quote(timeout),
    "--result", shlex.quote(result),
    "--cwd", shlex.quote(cwd),
    "--strategy-id", shlex.quote(strategy_id),
    "--strategy-sha256", shlex.quote(strategy_sha256),
    "--artifact-protocol-version", shlex.quote(artifact_protocol_version),
    "--publication-path", shlex.quote(publication_path),
    "--task-dir", shlex.quote(task_dir),
    "--attempt-id", shlex.quote(attempt_id),
    "--finalization-path", shlex.quote(finalization_path),
    "--finalization-timeout-seconds", shlex.quote(finalization_grace),
    "--deadline-path", shlex.quote(deadline_path),
    "--deadline-reminder-seconds", shlex.quote(deadline_reminder),
    "--shell-command", shlex.quote(command),
]))
PY
)"
        python3 "${PROTOCOL_CLI}" record-resume-fallback \
          --status-path "${STATUS_PATH}" \
          --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json" \
          --failure-path "${ATTEMPT_DIR}/runtime/RESUME_STARTUP_FAILURE.json" \
          --requested-session-id "${REQUESTED_SESSION_ID}" \
          --fallback-session-id "${FALLBACK_SESSION_ID}" \
          --reason "runtime_session_not_found" \
          --source runtime \
          --command "${FALLBACK_ORIGINAL_COMMAND}" \
          --supervisor-command "${FALLBACK_WORKER_COMMAND}"
        rm -f "${ATTEMPT_DIR}/runtime/STARTUP.json" \
          "${ATTEMPT_DIR}/runtime/SESSION.json" \
          "${ATTEMPT_DIR}/runtime/human-startup-probed" \
          "${ATTEMPT_DIR}/runtime/startup-pane.txt" \
          "${ATTEMPT_DIR}/runtime/DISPATCH_TIMEOUT.json" \
          "${SUPERVISOR_RESULT}"
        : > "${TRANSCRIPT_PATH}"
        RDO_EXECUTION_MODE="start"
        RDO_BACKEND_SESSION_ID="${FALLBACK_SESSION_ID}"
        RDO_WORKER_COMMAND="${FALLBACK_WORKER_COMMAND}"
        run_tmux_worker_once
      fi
    fi
  fi
  {
    echo "# Worker Result"
    echo
    echo "exit_code: ${EXIT_CODE_RAW}"
  } > "${ATTEMPT_DIR}/result.md"
fi

set +e
python3 "${PROTOCOL_CLI}" record-session \
  --status-path "${STATUS_PATH}" \
  --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json" \
  --session-path "${ATTEMPT_DIR}/runtime/SESSION.json"
SESSION_RECORD_CODE=$?
set -e
if [[ "${SESSION_RECORD_CODE}" -ne 0 ]]; then
  echo "record-session failed with exit ${SESSION_RECORD_CODE}; continuing to handoff validation" >> "${TRANSCRIPT_PATH}"
fi

if [[ -f "${ATTEMPT_DIR}/runtime/STARTUP.json" ]]; then
  STARTUP_STATE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "${ATTEMPT_DIR}/runtime/STARTUP.json")"
  append_event "worker_process_started"
  if [[ "${STARTUP_STATE}" == "prompt_dispatched" || \
        "${STARTUP_STATE}" == "worker_started" || \
        "${STARTUP_STATE}" == "prompt_submitted" || \
        "${STARTUP_STATE}" == "worker_waiting_for_user" || \
        "${STARTUP_STATE}" == "worker_startup_failed" ]]; then
    append_event "prompt_dispatched"
  fi
  if [[ "${STARTUP_STATE}" == "worker_started" ]]; then
    append_event "worker_started"
  elif [[ "${STARTUP_STATE}" == "worker_waiting_for_user" ]]; then
    : # Event was appended at detection time so the user sees it immediately.
  elif [[ "${STARTUP_STATE}" == "worker_startup_failed" || \
          "${STARTUP_STATE}" == "tui_startup_failed" ]]; then
    append_event "worker_startup_failed"
  fi
fi

if [[ "${DISPATCH_DRY_RUN}" != "1" ]]; then
  WORKTREE_AFTER_PATH="${ATTEMPT_DIR}/runtime/worktree-after.json"
  if [[ "${TASK_ARTIFACT_PROTOCOL_VERSION}" == "2" && -f "${WORKTREE_AFTER_PATH}" ]]; then
    WORKTREE_AFTER_CHECK="${ATTEMPT_DIR}/runtime/worktree-after-dispatch-check.json"
    python3 "${SCRIPT_DIR}/worktree_fingerprint.py" \
      --worktree "${WORKTREE_PATH}" \
      --output "${WORKTREE_AFTER_CHECK}"
    if ! json_files_equal "${WORKTREE_AFTER_PATH}" "${WORKTREE_AFTER_CHECK}"; then
      echo "task worktree changed after immutable v2 handoff publication" >> "${TRANSCRIPT_PATH}"
      EXIT_CODE=126
      EXIT_CODE_RAW="126"
    fi
    rm -f "${WORKTREE_AFTER_CHECK}"
  else
    python3 "${SCRIPT_DIR}/worktree_fingerprint.py" \
      --worktree "${WORKTREE_PATH}" \
      --output "${WORKTREE_AFTER_PATH}"
  fi
  if [[ "${RDO_ATTEMPT_PHASE}" == "planning" ]] && ! json_files_equal "${ATTEMPT_DIR}/runtime/worktree-before.json" "${ATTEMPT_DIR}/runtime/worktree-after.json"; then
    echo "planning worker modified the task worktree" >> "${TRANSCRIPT_PATH}"
    EXIT_CODE=126
    EXIT_CODE_RAW="126"
  elif [[ "${RDO_ATTEMPT_PHASE}" == "execution" ]] && ! python3 "${SCRIPT_DIR}/worktree_policy_check.py" \
      --before "${ATTEMPT_DIR}/runtime/worktree-before.json" \
      --after "${ATTEMPT_DIR}/runtime/worktree-after.json" \
      --strategy "${STRATEGY_PATH}" \
      --policy "${TASK_DIR}/EXECUTION_POLICY.json" \
      > "${ATTEMPT_DIR}/runtime/worktree-policy-result.json"; then
    echo "execution worker modified paths outside the approved strategy" >> "${TRANSCRIPT_PATH}"
    EXIT_CODE=126
    EXIT_CODE_RAW="126"
  fi
fi

if [[ -f "${SUPERVISOR_RESULT}" ]] && python3 - "${SUPERVISOR_RESULT}" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if payload.get("timed_out") is True else 1)
PY
then
  append_event "attempt_timed_out"
fi

set +e
python3 - "${SUPERVISOR_RESULT}" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing or unsafe supervisor result")
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2)
survivors = payload.get("surviving_pids")
healthy = (
    payload.get("cleanup_verified") is True
    and isinstance(survivors, list)
    and survivors == []
)
raise SystemExit(0 if healthy else 3)
PY
SUPERVISOR_HEALTH_CODE=$?
set -e
if [[ "${SUPERVISOR_HEALTH_CODE}" -ne 0 ]]; then
  PROCESS_CLEANUP_FAILED=1
  SUPERVISOR_CLEANUP_FAILED=1
  set +e
  cleanup_attempt_processes
  CLEANUP_RETRY_CODE=$?
  set -e
  if [[ "${CLEANUP_RETRY_CODE}" -eq 0 ]]; then
    PROCESS_CLEANUP_FAILED=0
  fi
fi

set +e
python3 "${PROTOCOL_CLI}" validate-handoff \
  --status-path "${STATUS_PATH}" \
  --attempt-id "${ATTEMPT_ID}" \
  --task-dir "${TASK_DIR}" \
  --attempt-path "${ATTEMPT_DIR}/ATTEMPT.json" \
  --startup-path "${ATTEMPT_DIR}/runtime/STARTUP.json" \
  --supervisor-result "${SUPERVISOR_RESULT}" \
  --worktree "${WORKTREE_PATH}" \
  --expected-profile "${TASK_PROFILE}" \
  --expected-task-id "${TASK_ID}" \
  --expected-artifact-protocol-version "${TASK_ARTIFACT_PROTOCOL_VERSION}" \
  --expected-phase "${RDO_ATTEMPT_PHASE}" \
  --expected-branch "${BRANCH}" \
  --expected-worktree "${WORKTREE_PATH}" \
  --expected-worker-backend "${RDO_WORKER_BACKEND}" \
  --expected-strategy-id "${STRATEGY_ID}" \
  --expected-strategy-revision "${STRATEGY_REVISION}" \
  --expected-strategy-sha256 "${STRATEGY_SHA256}" \
  --expected-backend-profile-sha256 "${BACKEND_PROFILE_SHA256}" \
  --expected-backend-settings-sha256 "${BACKEND_SETTINGS_SHA256}" \
  --expected-read-policy-sha256 "${READ_POLICY_SHA256}" \
  --expected-task-inputs-sha256 "${TASK_INPUTS_SHA256}" \
  --expected-task-base-commit "${TASK_BASE_COMMIT}" \
  --expected-worktree-before-sha256 "${WORKTREE_BEFORE_SHA256}" \
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
elif [[ "${FINAL_STATE}" == "verified" ]]; then
  append_event "worker_verified"
elif [[ "${FINAL_STATE}" == "strategy_review" ]]; then
  append_event "strategy_review_ready"
elif [[ "${FINAL_STATE}" == "blocked" ]]; then
  append_event "worker_blocked"
fi
