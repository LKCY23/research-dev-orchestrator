#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"

init_run_and_task smoke-run T001-status status-type-guard

attempt_id="A001-claude-test"
task_dir="${repo}/.agent-collab/runs/smoke-run/tasks/T001-status"
attempt_dir="${task_dir}/attempts/${attempt_id}"
mkdir -p "${attempt_dir}"
python3 "${RDO_ROOT}/scripts/protocol_cli.py" create-attempt \
  --path "${attempt_dir}/ATTEMPT.json" \
  --attempt-id "${attempt_id}" \
  --task-id T001-status \
  --agent-name test-worker \
  --worker-id W-test \
  --session-id "" \
  --phase execution \
  --command true \
  --cwd "${repo}" \
  --backend plain >/dev/null
printf '[]\n' > "${task_dir}/STATUS.json"

set +e
python3 "${RDO_ROOT}/scripts/protocol_cli.py" validate-handoff \
  --status-path "${task_dir}/STATUS.json" \
  --attempt-id "${attempt_id}" \
  --task-dir "${task_dir}" \
  --attempt-path "${attempt_dir}/ATTEMPT.json" \
  --exit-code-raw 0 > "${repo}/status-type.out" 2> "${repo}/status-type.err"
handoff_code="$?"
set -e
[[ "${handoff_code}" == "4" ]]
python3 - "${attempt_dir}/ATTEMPT.json" <<'PY'
import json
import sys

attempt = json.load(open(sys.argv[1], encoding="utf-8"))
assert attempt["state"] == "invalid_handoff"
assert attempt["handoff_valid"] is False
assert attempt["handoff_state"] is None
PY
grep -q "STATUS.json must be a JSON object" "${repo}/status-type.err"

python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id smoke-run \
  --task-id T002-attempt \
  --goal attempt-type-guard \
  --allowed-paths file.txt >/dev/null
task_dir="${repo}/.agent-collab/runs/smoke-run/tasks/T002-attempt"
attempt_id="A001-claude-test"
attempt_dir="${task_dir}/attempts/${attempt_id}"
mkdir -p "${attempt_dir}"
python3 - "${task_dir}/STATUS.json" "${attempt_id}" <<'PY'
import json
import sys
from datetime import datetime, timezone

status_path, attempt_id = sys.argv[1:3]
status = json.load(open(status_path, encoding="utf-8"))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status.update({
    "previous_state": "pending",
    "state": "running",
    "updated_at": now,
    "current_attempt_id": attempt_id,
})
status["state_history"] = [{"from": "pending", "to": "running", "actor": "dispatch", "at": now}]
json.dump(status, open(status_path, "w", encoding="utf-8"), indent=2)
PY
printf '[]\n' > "${attempt_dir}/ATTEMPT.json"

cat >> ".agent-collab/runs/smoke-run/EVENTS.ndjson" <<'JSON'
{"at":"2026-07-04T00:00:00Z","actor":"test","event":[],"run_id":"smoke-run"}
JSON

set +e
collect_json smoke-run "${repo}/type-guards.json"
set -e
assert_json_expr "${repo}/type-guards.json" "'T001-status: STATUS.json must be a JSON object' in '\\n'.join(payload['protocol_violations'])"
assert_json_expr "${repo}/type-guards.json" "'T002-attempt: ATTEMPT.json must be a JSON object' in '\\n'.join(payload['protocol_violations'])"
assert_json_expr "${repo}/type-guards.json" "'EVENTS.ndjson line' in '\\n'.join(payload['protocol_violations']) and 'event must be a non-empty string' in '\\n'.join(payload['protocol_violations'])"
