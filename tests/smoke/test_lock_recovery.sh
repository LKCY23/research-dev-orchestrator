#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_run_and_task smoke-run T001-lock lock

task=".agent-collab/runs/smoke-run/tasks/T001-lock"
mkdir -p "${task}/.dispatch-lock"
printf 'A001-test\n' > "${task}/.dispatch-lock/attempt_id"
printf '999999\n' > "${task}/.dispatch-lock/pid"
python3 - <<'PY'
import json
from pathlib import Path

status_path = Path(".agent-collab/runs/smoke-run/tasks/T001-lock/STATUS.json")
status = json.load(open(status_path, encoding="utf-8"))
status["current_attempt_id"] = "A001-test"
status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
PY

set +e
python3 "${RDO_ROOT}/scripts/remove_dispatch_lock.py" --run-id smoke-run --task-id T001-lock --reason "smoke" >/tmp/rdo-lock-dryrun.txt
dry_code="$?"
set -e
[[ "${dry_code}" == "1" ]]
[[ -d "${task}/.dispatch-lock" ]]

python3 "${RDO_ROOT}/scripts/remove_dispatch_lock.py" --run-id smoke-run --task-id T001-lock --reason "smoke" --confirmed >/tmp/rdo-lock-confirmed.txt
[[ ! -e "${task}/.dispatch-lock" ]]
grep -q '"event": "dispatch_lock_removed"' .agent-collab/runs/smoke-run/EVENTS.ndjson
find .agent-collab/runs/smoke-run/diagnostics -name recovery-operation.json | grep -q recovery-operation.json
