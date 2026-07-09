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
branch_prefix = "config/"
worktree_root = "config-worktrees"

[runtime]
backend = "plain"
TOML

RDO_TASK_BRANCH_PREFIX="env/" \
RDO_WORKTREE_ROOT="env-worktrees" \
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id smoke-run \
  --task-id T001-env \
  --goal env \
  --allowed-paths file.txt >/dev/null

RDO_RUNTIME_BACKEND=tmux python3 "${RDO_ROOT}/scripts/config_cli.py" export-env > "${repo}/env.out"

python3 - <<'PY'
import json
from pathlib import Path

status = json.load(open(".agent-collab/runs/smoke-run/tasks/T001-env/STATUS.json", encoding="utf-8"))
assert status["branch"] == "env/T001-env", status
assert status["worktree"] == "env-worktrees/T001-env", status
text = Path("env.out").read_text(encoding="utf-8")
assert "RDO_RUNTIME_BACKEND=tmux" in text, text
assert "RDO_WORKER_BACKEND=claude-code" in text, text
PY
