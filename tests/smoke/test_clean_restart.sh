#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
failed_worker="${repo}/worker-fail.sh"
verified_worker="${repo}/worker-verified.sh"

cat > "${failed_worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
printf 'partial from failed attempt\n' > file.txt
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --state blocked \
  --summary "restart requested after partial work" \
  --blocker-type needs_coordinator \
  --blocking-reason "retry from a clean workspace" >/dev/null
SH
chmod +x "${failed_worker}"
make_verified_worker "${verified_worker}"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id restart-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id restart-run \
  --task-id T001-restart \
  --goal restart \
  --profile direct \
  --allowed-paths file.txt >/dev/null
complete_task_contract restart-run T001-restart restart

cat > .agent-collab/rdo.toml <<TOML
[worker]
command = "/bin/bash ${failed_worker}"

[runtime]
backend = "plain"
io_mode = "machine"
TOML

"${RDO_ROOT}/scripts/dispatch_claude.sh" restart-run T001-restart \
  > "${repo}/first.out" 2> "${repo}/first.err"

task="${repo}/.agent-collab/runs/restart-run/tasks/T001-restart"
old_worktree="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worktree"])' "${task}/STATUS.json")"
if [[ "${old_worktree}" != /* ]]; then
  old_worktree="${repo}/${old_worktree}"
fi

cat > .agent-collab/rdo.toml <<TOML
[worker]
command = "/bin/bash ${verified_worker}"

[runtime]
backend = "plain"
io_mode = "machine"
TOML

python3 "${RDO_ROOT}/scripts/rdo.py" task resume \
  --task-dir "${task}" \
  --execution-mode restart > "${repo}/restart.out"

python3 - "${task}" "${old_worktree}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
old_worktree = Path(sys.argv[2])
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
attempts = sorted(path for path in (task / "attempts").iterdir() if path.is_dir())
current = json.loads((attempts[-1] / "ATTEMPT.json").read_text(encoding="utf-8"))
new_worktree = Path(status["worktree"])
if not new_worktree.is_absolute():
    new_worktree = task.parents[4] / new_worktree

assert len(attempts) == 2, attempts
assert status["task_id"] == "T001-restart", status
assert status["state"] == "verified", status
assert current["execution_mode"] == "restart", current
assert current["parent_attempt_id"] == attempts[0].name, current
assert (attempts[-1] / "runtime" / "CLEAN_RESTART.json").is_file()
assert (old_worktree / "file.txt").read_text() == "partial from failed attempt\n"
assert (new_worktree / "file.txt").read_text() == "hello\n"
assert old_worktree.resolve() != new_worktree.resolve()
PY
