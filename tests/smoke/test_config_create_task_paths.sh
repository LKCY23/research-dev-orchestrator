#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id smoke-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
cat > .agent-collab/rdo.toml <<'TOML'
[task]
branch_prefix = "work/"
worktree_root = "custom-worktrees"
TOML

python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id smoke-run \
  --task-id T001-paths \
  --goal paths \
  --allowed-paths file.txt >/dev/null

python3 - <<'PY'
import json

status = json.load(open(".agent-collab/runs/smoke-run/tasks/T001-paths/STATUS.json", encoding="utf-8"))
assert status["branch"] == "work/T001-paths", status
assert status["worktree"] == "custom-worktrees/T001-paths", status
PY
