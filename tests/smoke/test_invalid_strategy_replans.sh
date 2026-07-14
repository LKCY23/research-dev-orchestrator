#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_run_and_task invalid-strategy-run T001-invalid-strategy recovery
task="${repo}/.agent-collab/runs/invalid-strategy-run/tasks/T001-invalid-strategy"

python3 - "${task}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

task = Path(sys.argv[1])
status_path = task / "STATUS.json"
status = json.loads(status_path.read_text(encoding="utf-8"))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status.update(previous_state="review", state="changes_requested", owner="coordinator", updated_at=now)
status["state_history"].append(
    {"from": "review", "to": "changes_requested", "actor": "coordinator", "at": now}
)
status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

strategy_path = task / "strategy" / "STRATEGY-v001.json"
strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
strategy["workflows"][0]["kind"] = "review"
strategy_path.write_text(json.dumps(strategy, indent=2) + "\n", encoding="utf-8")
PY

prompt_capture="${repo}/planning-prompt.txt"
worker="${repo}/capture-planning.sh"
cat > "${worker}" <<SH
#!/usr/bin/env bash
cat > "${prompt_capture}"
exit 1
SH
chmod +x "${worker}"

set +e
RDO_WORKER_COMMAND="${worker}" "${RDO_ROOT}/scripts/dispatch_agent.sh" \
  invalid-strategy-run T001-invalid-strategy >/dev/null 2>&1
dispatch_status=$?
set -e
test "${dispatch_status}" -ne 0
grep -q "## Planning Phase" "${prompt_capture}"
grep -q "strategy revise --task-dir" "${prompt_capture}"

python3 - "${task}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
attempt = json.loads(
    (task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json").read_text(
        encoding="utf-8"
    )
)
assert attempt["phase"] == "planning", attempt
assert status["state"] == "blocked", status
PY
