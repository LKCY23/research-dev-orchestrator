#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: scripts/dispatch_agent.sh <run-id> <task-id> [options]

Options:
  --worker <backend>        claude-code | codex | opencode | kimi-code
  --runtime <backend>       plain | tmux
  --io <mode>               machine | human
  --permission <mode>       default | auto | yolo
  --agent-name <name>       worker display name
  --session-id <id>         backend session id for manual resume metadata
  --worker-id <id>          stable logical worker id (normally auto-detected)
  --execution-mode <mode>   start | resume | replace (normally auto-detected)
  --command <shell-command> explicit command override, mainly for tests
  --phase <phase>           planning | execution (auto-detected when omitted)
EOF
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

RUN_ID="$1"
TASK_ID="$2"
shift 2

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker)
      export RDO_WORKER_BACKEND="$2"
      shift 2
      ;;
    --runtime)
      export RDO_RUNTIME_BACKEND="$2"
      shift 2
      ;;
    --io)
      export RDO_IO_MODE="$2"
      shift 2
      ;;
    --permission)
      export RDO_PERMISSION_MODE="$2"
      shift 2
      ;;
    --agent-name)
      export RDO_WORKER_AGENT_NAME="$2"
      shift 2
      ;;
    --session-id)
      export RDO_BACKEND_SESSION_ID="$2"
      shift 2
      ;;
    --worker-id)
      export RDO_WORKER_ID="$2"
      shift 2
      ;;
    --execution-mode)
      export RDO_EXECUTION_MODE="$2"
      shift 2
      ;;
    --command)
      export RDO_WORKER_COMMAND="$2"
      shift 2
      ;;
    --phase)
      export RDO_ATTEMPT_PHASE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/dispatch_claude.sh" "${RUN_ID}" "${TASK_ID}"
