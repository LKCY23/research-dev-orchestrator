#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"

worker="${repo}/kill-supervisor-worker.sh"
descendant_pid_path="${repo}/descendant.pid"
late_sentinel="${repo}/late-descendant-write.txt"

cat > "${worker}" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

prompt="$(mktemp)"
cat > "${prompt}"
ATTEMPT_DIR="$(awk -F': ' '/^- ATTEMPT_DIR:/ {print $2}' "${prompt}")"
supervisor_state="${ATTEMPT_DIR}/runtime/supervisor.json"

python3 - "${supervisor_state}" "${DESCENDANT_PID_PATH}" "${LATE_SENTINEL}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
import time

state_path = Path(sys.argv[1])
pid_path = Path(sys.argv[2])
sentinel = Path(sys.argv[3])

deadline = time.monotonic() + 3.0
while True:
    try:
        worker_pid = json.loads(state_path.read_text(encoding="utf-8"))["worker_pid"]
        break
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.01)

supervisor_pid = int(
    subprocess.check_output(
        ["ps", "-o", "ppid=", "-p", str(worker_pid)],
        text=True,
    ).strip()
)

child = (
    "import os,pathlib,signal,sys,time;"
    "pid_path=pathlib.Path(sys.argv[2]);"
    "pid_path.write_text(str(os.getpid())+'\\n',encoding='utf-8');"
    "time.sleep(0.1);"
    "os.kill(int(sys.argv[1]),signal.SIGKILL);"
    "time.sleep(1.5);"
    "pathlib.Path(sys.argv[3]).write_text('cleanup failed\\n',encoding='utf-8');"
    "time.sleep(30)"
)
subprocess.Popen(
    [sys.executable, "-c", child, str(supervisor_pid), str(pid_path), str(sentinel)],
    start_new_session=True,
)
PY

sleep 30
SH
chmod +x "${worker}"

init_run_and_task missing-supervisor-run T001-missing-supervisor missing-supervisor
task="${repo}/.agent-collab/runs/missing-supervisor-run/tasks/T001-missing-supervisor"

set +e
DESCENDANT_PID_PATH="${descendant_pid_path}" \
LATE_SENTINEL="${late_sentinel}" \
RDO_RUNTIME_BACKEND=plain \
RDO_IO_MODE=machine \
RDO_WORKER_COMMAND="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" \
  missing-supervisor-run T001-missing-supervisor \
  >"${repo}/dispatch.out" 2>"${repo}/dispatch.err"
dispatch_code="$?"
set -e

[[ "${dispatch_code}" -ne 0 ]]
[[ -s "${descendant_pid_path}" ]]
descendant_pid="$(tr -d '[:space:]' < "${descendant_pid_path}")"
[[ "${descendant_pid}" =~ ^[0-9]+$ ]]

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! kill -0 "${descendant_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
! kill -0 "${descendant_pid}" 2>/dev/null
sleep 1.7
test ! -e "${late_sentinel}"

python3 - "${task}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text(encoding="utf-8"))
cleanup = json.loads(
    (attempt_dir / "runtime" / "CLEANUP.json").read_text(encoding="utf-8")
)

assert status["state"] == "blocked", status
assert status["needs_coordinator"] is True, status
assert status["state"] not in {"review", "verified", "approved", "merged"}, status
assert attempt["state"] == "invalid_handoff", attempt
assert attempt["handoff_valid"] is False, attempt
assert cleanup["terminated"] is True, cleanup
assert cleanup["cleanup_verified"] is True, cleanup
assert cleanup["surviving_pids"] == [], cleanup
assert (task / ".dispatch-lock").is_dir()
assert not (attempt_dir / "supervisor-result.json").exists()
PY
