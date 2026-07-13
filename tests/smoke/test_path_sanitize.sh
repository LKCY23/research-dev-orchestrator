#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

parent="$(mktemp -d)"
repo="${parent}/repo with spaces"
setup_smoke_repo "${repo}" >/dev/null
cd "${repo}"
worker="$(mktemp /tmp/rdo-worker-review.XXXXXX)"
make_review_worker "${worker}"

python3 "${RDO_ROOT}/scripts/init_run.py" --run-id "smoke:run" --project-slug smoke --objective smoke --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" --run-id "smoke:run" --task-id T001-colon --goal colon --allowed-paths file.txt >/dev/null
seed_approved_strategy "smoke:run" T001-colon
RDO_WORKER_BACKEND=tmux RDO_TMUX_SESSION_PREFIX="rdo:bad" CLAUDE_CODE_CMD="${worker}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" "smoke:run" T001-colon

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke:run/tasks/T001-colon")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
session = attempt["runtime"]["tmux_session"]
assert ":" not in session, session
assert status["state"] == "review"
PY
