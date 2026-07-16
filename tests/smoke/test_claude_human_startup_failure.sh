#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
fake_bin="${repo}/fake-bin"
mkdir -p "${fake_bin}"
cat > "${fake_bin}/claude" <<'SH'
#!/usr/bin/env bash
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
echo "WARNING: Claude Code running in Bypass Permissions mode"
echo "Yes, I accept"
sleep 30
SH
chmod +x "${fake_bin}/claude"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id human-startup-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id human-startup-run \
  --task-id T001-human-startup \
  --goal startup \
  --profile delegated \
  --allowed-paths file.txt >/dev/null
complete_task_contract human-startup-run T001-human-startup startup

set +e
PATH="${fake_bin}:${PATH}" \
RDO_RUNTIME_BACKEND=tmux \
RDO_IO_MODE=human \
RDO_PERMISSION_MODE=yolo \
RDO_TMUX_WAIT_TIMEOUT_SECONDS=10 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" \
  human-startup-run T001-human-startup \
  > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
code=$?
set -e
[[ "${code}" -eq 4 ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/human-startup-run/tasks/T001-human-startup")
status = json.loads((task / "STATUS.json").read_text())
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
startup = json.loads((attempt_dir / "runtime" / "STARTUP.json").read_text())
assert startup["state"] == "tui_startup_failed", startup
assert startup["failure"]["code"] == "permission_confirmation_required", startup
assert attempt["outcome"] == "startup_failed", attempt
assert status["state"] == "blocked", status
assert status["blocker_type"] == "needs_user", status
assert not (task / ".dispatch-lock").exists()
PY
