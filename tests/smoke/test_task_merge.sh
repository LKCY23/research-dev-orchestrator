#!/usr/bin/env bash

set -euo pipefail

source "$(dirname "$0")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_raw_run_and_task merge-run T101-merge "merge smoke"

task="${repo}/.agent-collab/runs/merge-run/tasks/T101-merge"
worktree="${repo}/.agent-worktrees/T101-merge"
git branch agent/T101-merge
git worktree add "${worktree}" agent/T101-merge >/dev/null
printf 'merged content\n' > "${worktree}/file.txt"
git -C "${worktree}" add file.txt
git -C "${worktree}" commit -m task >/dev/null

python3 - "${task}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
status_path = task / "STATUS.json"
status = json.loads(status_path.read_text())
status.update(
    profile="delegated",
    state="review",
    previous_state="running",
    owner="dispatch",
    current_attempt_id="A001-smoke",
)
status["state_history"] = [
    {"from": "pending", "to": "running", "actor": "dispatch", "at": "2026-07-14T00:00:00Z"},
    {"from": "running", "to": "review", "actor": "dispatch", "at": "2026-07-14T00:01:00Z"},
]
status_path.write_text(json.dumps(status, indent=2) + "\n")
(task / "EVIDENCE.md").write_text("# Evidence\n\nPassed.\n")
(task / "HANDOFF.json").write_text(json.dumps({"requested_state": "review"}) + "\n")
(task / "reviews").mkdir(exist_ok=True)
(task / "reviews" / "findings.md").write_text("# Findings\n\nNo findings.\n")
PY

python3 "${RDO_ROOT}/scripts/rdo.py" task review \
  --task-dir "${task}" \
  --decision approved \
  --reviewer smoke \
  --findings-file "${task}/reviews/findings.md" >/dev/null

commit="$(git -C "${worktree}" rev-parse HEAD)"
python3 "${RDO_ROOT}/scripts/rdo.py" task merge \
  --task-dir "${task}" \
  --target-worktree "${repo}" \
  --expected-commit "${commit}" \
  --verify-command "/bin/test -f file.txt" \
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
PY
