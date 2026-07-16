#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

set_attempt_timeout() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["attempt_wall_seconds"] = int(sys.argv[2])
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

repo="$(setup_smoke_repo)"
cd "${repo}"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id grace-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id grace-run \
  --task-id T001-grace \
  --goal grace \
  --profile direct \
  --allowed-paths file.txt >/dev/null
complete_task_contract grace-run T001-grace grace
grace_task="${repo}/.agent-collab/runs/grace-run/tasks/T001-grace"
set_attempt_timeout "${grace_task}/EXECUTION_POLICY.json" 2

grace_worker="${repo}/grace-worker.sh"
cat > "${grace_worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
printf 'completed before finalization\\n' > file.txt
python3 "${RDO_ROOT}/scripts/rdo.py" finalization begin \
  --attempt-dir "\${ATTEMPT_DIR}" >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --check-id smoke >/dev/null
sleep 2.1
git add file.txt
git commit -m "commit during finalize-only grace" >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --state verified \
  --self-review-passed \
  --summary "completed within independent finalization grace" >/dev/null
SH
chmod +x "${grace_worker}"

RDO_WORKER_COMMAND="${grace_worker}" \
RDO_FINALIZATION_GRACE_SECONDS=3 \
RDO_DEADLINE_REMINDER_SECONDS=1 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" grace-run T001-grace >/dev/null

python3 - "${grace_task}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
attempt = task / "attempts" / status["current_attempt_id"]
supervisor = json.loads((attempt / "supervisor-result.json").read_text())
deadline = json.loads((attempt / "runtime" / "DEADLINE.json").read_text())
assert status["state"] == "verified", status
assert supervisor["timed_out"] is False, supervisor
assert supervisor["finalization_started"] is True, supervisor
assert supervisor["elapsed_seconds"] > deadline["attempt_wall_seconds"], supervisor
assert json.loads((attempt / "runtime" / "supervisor.json").read_text())[
    "deadline"
]["phase"] == "finalization"
PY

resume_repo="$(setup_smoke_repo)"
cd "${resume_repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id resume-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id resume-run \
  --task-id T002-resume \
  --goal resume \
  --profile direct \
  --allowed-paths file.txt >/dev/null
complete_task_contract resume-run T002-resume resume
resume_task="${resume_repo}/.agent-collab/runs/resume-run/tasks/T002-resume"
set_attempt_timeout "${resume_task}/EXECUTION_POLICY.json" 3

bad_worker="${resume_repo}/bad-finalization-worker.sh"
cat > "${bad_worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
python3 "${RDO_ROOT}/scripts/rdo.py" finalization begin \
  --attempt-dir "\${ATTEMPT_DIR}" >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --check-id smoke >/dev/null
printf 'recoverable committed work\\n' > file.txt
git add file.txt
git commit -m "recoverable work after finalization entry" >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --state verified \
  --self-review-passed \
  --summary "must be rejected" >/dev/null
SH
chmod +x "${bad_worker}"

set +e
RDO_WORKER_COMMAND="${bad_worker}" \
RDO_FINALIZATION_GRACE_SECONDS=2 \
RDO_DEADLINE_REMINDER_SECONDS=1 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" resume-run T002-resume >/dev/null
bad_code="$?"
set -e
[[ "${bad_code}" == "4" ]]

read -r first_attempt first_worktree first_head <<<"$(python3 - "${resume_task}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
attempt_id = status["current_attempt_id"]
attempt = json.loads((task / "attempts" / attempt_id / "ATTEMPT.json").read_text())
worktree = Path(attempt["runtime"]["cwd"])
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
assert status["state"] == "blocked", status
assert status["blocker_type"] == "needs_coordinator", status
assert attempt["outcome"] == "finalization_failed", attempt
assert not (task / "attempts" / attempt_id / "HANDOFF.json").exists()
print(attempt_id, worktree, head)
PY
)"

resume_worker="${resume_repo}/resume-worker.sh"
cat > "${resume_worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
test "\$(cat file.txt)" = "recoverable committed work"
python3 "${RDO_ROOT}/scripts/rdo.py" finalization begin \
  --attempt-dir "\${ATTEMPT_DIR}" >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --check-id smoke >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --state verified \
  --self-review-passed \
  --summary "reused prior worktree and commit" >/dev/null
SH
chmod +x "${resume_worker}"

RDO_WORKER_COMMAND="${resume_worker}" \
RDO_FINALIZATION_GRACE_SECONDS=2 \
RDO_DEADLINE_REMINDER_SECONDS=1 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" resume-run T002-resume >/dev/null

python3 - "${resume_task}" "${first_attempt}" "${first_worktree}" "${first_head}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

task = Path(sys.argv[1])
first_attempt, first_worktree, first_head = sys.argv[2:]
status = json.loads((task / "STATUS.json").read_text())
second_id = status["current_attempt_id"]
second = json.loads((task / "attempts" / second_id / "ATTEMPT.json").read_text())
worktree = Path(second["runtime"]["cwd"])
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
assert status["state"] == "verified", status
assert second_id != first_attempt
assert second["parent_attempt_id"] == first_attempt, second
assert str(worktree) == first_worktree
assert head == first_head
assert (worktree / "file.txt").read_text() == "recoverable committed work\n"
PY

timeout_repo="$(setup_smoke_repo)"
cd "${timeout_repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id timeout-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id timeout-run \
  --task-id T003-timeout \
  --goal timeout \
  --profile direct \
  --allowed-paths file.txt >/dev/null
complete_task_contract timeout-run T003-timeout timeout
timeout_task="${timeout_repo}/.agent-collab/runs/timeout-run/tasks/T003-timeout"
set_attempt_timeout "${timeout_task}/EXECUTION_POLICY.json" 1
late_sentinel="${timeout_repo}/late-descendant.txt"

timeout_worker="${timeout_repo}/timeout-worker.sh"
cat > "${timeout_worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
python3 "${RDO_ROOT}/scripts/rdo.py" finalization begin \
  --attempt-dir "\${ATTEMPT_DIR}" >/dev/null
python3 - "${late_sentinel}" <<'PY'
import pathlib
import signal
import subprocess
import sys
import time

sentinel = sys.argv[1]
child = (
    "import pathlib,time; time.sleep(.7); "
    f"pathlib.Path({sentinel!r}).write_text('late')"
)
signal.signal(
    signal.SIGINT,
    lambda *_: subprocess.Popen(
        [sys.executable, "-c", child],
        start_new_session=True,
    ),
)
while True:
    time.sleep(.05)
PY
SH
chmod +x "${timeout_worker}"

set +e
RDO_WORKER_COMMAND="${timeout_worker}" \
RDO_FINALIZATION_GRACE_SECONDS=1 \
RDO_DEADLINE_REMINDER_SECONDS=1 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" timeout-run T003-timeout >/dev/null
timeout_code="$?"
set -e
[[ "${timeout_code}" == "4" ]]
sleep 1

python3 - "${timeout_task}" "${late_sentinel}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
sentinel = Path(sys.argv[2])
status = json.loads((task / "STATUS.json").read_text())
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
supervisor = json.loads((attempt_dir / "supervisor-result.json").read_text())
assert status["state"] == "blocked", status
assert attempt["outcome"] == "finalization_timed_out", attempt
assert supervisor["timeout_phase"] == "finalization", supervisor
assert supervisor["surviving_pids"] == [], supervisor
assert supervisor["cleanup_verified"] is True, supervisor
assert not sentinel.exists(), sentinel
PY
