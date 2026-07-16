#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" --run-id direct-run --project-slug smoke --objective smoke --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" --run-id direct-run --task-id T001-direct --goal direct --profile direct --allowed-paths file.txt >/dev/null
complete_task_contract direct-run T001-direct direct
worker="${repo}/direct-worker.sh"
make_verified_worker "${worker}"
RDO_WORKER_COMMAND="${worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" direct-run T001-direct >/dev/null
python3 "${RDO_ROOT}/scripts/collect_status.py" --run-id direct-run --json > "${repo}/direct-status.json"
assert_json_expr "${repo}/direct-status.json" "payload['valid'] is True"

python3 - "${repo}/.agent-collab/runs/direct-run/tasks/T001-direct" <<'PY'
import json, subprocess, sys
from pathlib import Path
task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
assert status["profile"] == "direct"
assert status["state"] == "verified", status
attempt = json.loads((task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json").read_text())
attempt_dir = task / "attempts" / status["current_attempt_id"]
assert attempt["phase"] == "execution"
assert attempt["strategy_id"] is None
assert attempt["worker_id"] == status["assigned_worker"]["worker_id"]
assert attempt["execution_mode"] == "start"
task_head = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=attempt["runtime"]["cwd"], text=True
).strip()
handoff = json.loads((attempt_dir / "HANDOFF.json").read_text())
ready = json.loads((attempt_dir / "runtime" / "HANDOFF_READY.json").read_text())
assert attempt["verified_commit"] == task_head
assert handoff["source_commit"] == task_head
assert ready["source_commit"] == task_head
assert ready["requested_state"] == "verified"
assert not (task / "HANDOFF.json").exists()
policy = json.loads((task / "EXECUTION_POLICY.json").read_text())
assert policy["strategy_required"] is False
PY

late_repo="$(setup_smoke_repo)"
cd "${late_repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" --run-id direct-late-run --project-slug smoke --objective smoke --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" --run-id direct-late-run --task-id T002-direct-late --goal direct --profile direct --allowed-paths file.txt >/dev/null
complete_task_contract direct-late-run T002-direct-late direct
late_worker="${late_repo}/direct-late-worker.sh"
cat > "${late_worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --check-id smoke >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --state verified \
  --summary "self-reviewed commit A" \
  --self-review-passed >/dev/null
chmod +x file.txt
git add file.txt
git commit -m "commit B after finalize" >/dev/null
SH
chmod +x "${late_worker}"
set +e
RDO_WORKER_COMMAND="${late_worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" direct-late-run T002-direct-late >/dev/null
late_code="$?"
set -e
[[ "${late_code}" == "4" ]]
python3 - "${late_repo}/.agent-collab/runs/direct-late-run/tasks/T002-direct-late" <<'PY'
import json, subprocess, sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
attempt = json.loads(
    (task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json").read_text()
)
attempt_dir = task / "attempts" / status["current_attempt_id"]
handoff = json.loads((attempt_dir / "HANDOFF.json").read_text())
assert status["state"] == "blocked", status
current_head = subprocess.check_output(
    ["git", "rev-parse", "HEAD"],
    cwd=attempt["runtime"]["cwd"],
    text=True,
).strip()
assert current_head != handoff["source_commit"]
assert (
    "HEAD changed after handoff finalization" in status["blocking_reason"]
    or "source_commit" in status["blocking_reason"]
), status
assert attempt["state"] == "invalid_handoff"
assert attempt["handoff_valid"] is False
assert "verified_commit" not in attempt
assert handoff["summary"] == "self-reviewed commit A"
assert (attempt_dir / "runtime" / "HANDOFF_READY.json").is_file()
assert not (task / "HANDOFF.json").exists()
PY
