#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" --run-id delegated-run --project-slug smoke --objective smoke --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" --run-id delegated-run --task-id T001-delegated --goal delegated --profile delegated --allowed-paths file.txt >/dev/null
worker="${repo}/delegated-worker.sh"
make_review_worker "${worker}"
RDO_WORKER_COMMAND="${worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" delegated-run T001-delegated >/dev/null

task="${repo}/.agent-collab/runs/delegated-run/tasks/T001-delegated"
python3 - "${task}/STATUS.json" <<'PY'
import json, sys
from datetime import datetime, timezone
path = sys.argv[1]
status = json.load(open(path, encoding="utf-8"))
assert status["state"] == "review"
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status["previous_state"] = "review"
status["state"] = "changes_requested"
status["updated_at"] = now
status["state_history"].append({"from": "review", "to": "changes_requested", "actor": "coordinator", "at": now})
json.dump(status, open(path, "w", encoding="utf-8"), indent=2)
PY
RDO_WORKER_COMMAND="${worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" delegated-run T001-delegated >/dev/null

python3 - "${task}" <<'PY'
import json, sys
from pathlib import Path
task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
attempts = sorted((task / "attempts").glob("*/ATTEMPT.json"))
assert len(attempts) == 2
first, second = [json.loads(path.read_text()) for path in attempts]
assert status["state"] == "review"
assert first["worker_id"] == second["worker_id"]
assert first["session_id"] == second["session_id"]
assert second["parent_attempt_id"] == first["attempt_id"]
assert second["execution_mode"] == "resume"
assert second["resume_reason"] == "changes_requested"
PY
