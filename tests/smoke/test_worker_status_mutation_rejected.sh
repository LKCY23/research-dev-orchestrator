#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-mutates-status.sh"
cat > "${worker}" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
prompt="$(mktemp)"
cat > "${prompt}"
TASK_DIR="$(awk -F': ' '/^- TASK_DIR:/ {print $2}' "${prompt}")"
STATUS_PATH="${TASK_DIR}/STATUS.json"
ATTEMPT_DIR="$(awk -F': ' '/^- ATTEMPT_DIR:/ {print $2}' "${prompt}")"
python3 "${RDO_ROOT}/scripts/rdo.py" check \
  --attempt-dir "${ATTEMPT_DIR}" \
  --check-id smoke >/dev/null
python3 - "${STATUS_PATH}" <<'PY'
import json
import sys
from datetime import datetime, timezone

path = sys.argv[1]
status = json.load(open(path, encoding="utf-8"))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status["previous_state"] = status["state"]
status["state"] = "review"
status["updated_at"] = now
status.setdefault("state_history", []).append({"from": "running", "to": "review", "actor": "claude-code", "at": now})
json.dump(status, open(path, "w", encoding="utf-8"), indent=2)
PY
SH
chmod +x "${worker}"

init_run_and_task smoke-run T001-mutate mutate
set +e
CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-mutate
code="$?"
set -e
[[ "${code}" == "4" ]]

collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['state'] == 'blocked'"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['blocker_type'] == 'needs_coordinator'"

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-mutate")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
attempt = json.load(open(task / "attempts" / status["current_attempt_id"] / "ATTEMPT.json", encoding="utf-8"))
assert status["state_history"][-1]["from"] == "running"
assert status["state_history"][-1]["to"] == "blocked"
assert status["state_history"][-1]["actor"] == "dispatch"
assert all(item.get("actor") != "claude-code" for item in status["state_history"])
assert attempt["state"] == "invalid_handoff"
PY
