#!/usr/bin/env bash

set -euo pipefail

source "$(dirname "$0")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id merge-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id merge-run \
  --task-id T101-merge \
  --goal "merge smoke" \
  --profile delegated \
  --allowed-paths file.txt >/dev/null
complete_task_contract merge-run T101-merge "merge smoke"

task="${repo}/.agent-collab/runs/merge-run/tasks/T101-merge"
worker="${repo}/merge-worker.sh"
cat > "${worker}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
ATTEMPT_DIR="\$(awk -F': ' '/^- ATTEMPT_DIR:/ {print \$2}' "\${prompt}")"
printf 'merged content\n' > file.txt
git add file.txt
git commit -m task >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --check-id smoke >/dev/null
python3 "${RDO_ROOT}/scripts/rdo.py" finalize \
  --attempt-dir "\${ATTEMPT_DIR}" \
  --state review \
  --summary "merge smoke completed" >/dev/null
SH
chmod +x "${worker}"
RDO_WORKER_COMMAND="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" merge-run T101-merge >/dev/null
rm -f "${worker}"

mkdir -p "${task}/reviews"
printf '# Findings\n\nNo findings.\n' > "${task}/reviews/findings.md"

python3 "${RDO_ROOT}/scripts/rdo.py" task review \
  --task-dir "${task}" \
  --decision approved \
  --reviewer smoke \
  --findings-file "${task}/reviews/findings.md" >/dev/null

worktree="${repo}/.agent-worktrees/T101-merge"
commit="$(git -C "${worktree}" rev-parse HEAD)"
python3 "${RDO_ROOT}/scripts/rdo.py" task merge \
  --task-dir "${task}" \
  --target-worktree "${repo}" \
  --expected-commit "${commit}" \
  --coordinator smoke >/dev/null

# A repeated invocation must be a no-op and must not duplicate task_merged.
python3 "${RDO_ROOT}/scripts/rdo.py" task merge \
  --task-dir "${task}" \
  --target-worktree "${repo}" \
  --expected-commit "${commit}" \
  --coordinator smoke >/dev/null

python3 - "${task}" "${commit}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
commit = sys.argv[2]
status = json.loads((task / "STATUS.json").read_text())
assert status["state"] == "merged", status
events = [
    json.loads(line)
    for line in (task.parent.parent / "EVENTS.ndjson").read_text().splitlines()
]
merged = [item for item in events if item.get("event") == "task_merged"]
assert len(merged) == 1, merged
assert merged[0]["commit"] == commit, merged[0]
assert merged[0]["verification"]["passed"] is True, merged[0]
decision = json.loads((task / "reviews" / "DECISION-v001.json").read_text())
assert decision["approved_commit"] == commit, decision
attempt_id = status["current_attempt_id"]
ready = json.loads((task / "attempts" / attempt_id / "runtime" / "HANDOFF_READY.json").read_text())
assert ready["source_commit"] == commit
assert decision["artifact_binding"]["ready_sha256"]
assert merged[0]["artifact_binding"] == decision["artifact_binding"]
PY
