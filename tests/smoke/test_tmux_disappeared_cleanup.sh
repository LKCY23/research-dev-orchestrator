#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-drop-session.sh"
sentinel="${repo}/late-after-session.txt"
cat > "${worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
python3 - "${sentinel}" <<'PY'
import subprocess
import sys

child = (
    "import pathlib,time; time.sleep(2); "
    f"pathlib.Path({sys.argv[1]!r}).write_text('late')"
)
subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
PY
sleep 0.5
session="\$(tmux display-message -p '#S')"
tmux kill-session -t "\${session}"
sleep 30
SH
chmod +x "${worker}"

init_run_and_task smoke-run T001-disappeared disappeared
set +e
RDO_WORKER_BACKEND=tmux RDO_IO_MODE=human RDO_TMUX_WAIT_TIMEOUT_SECONDS=1 CLAUDE_CODE_CMD="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-disappeared
code="$?"
set -e
[[ "${code}" == "5" ]]
sleep 3
test ! -e "${sentinel}"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-disappeared")
status = json.loads((task / "STATUS.json").read_text())
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
cleanup = json.loads((attempt_dir / "runtime" / "CLEANUP.json").read_text())
assert status["state"] == "blocked", status
assert status["blocker_type"] == "budget", status
assert attempt["outcome"] == "timed_out_unfinalized", attempt
assert cleanup["terminated"] is True, cleanup
assert cleanup["surviving_pids"] == [], cleanup
assert not (task / ".dispatch-lock").exists()
PY
