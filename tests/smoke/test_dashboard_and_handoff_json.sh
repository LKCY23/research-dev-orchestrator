#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-dashboard dashboard
test -f ".agent-collab/runs/smoke-run/tasks/T001-dashboard/HANDOFF.json"

CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-dashboard
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['handoff_index']['summary'] == 'smoke worker completed'"

python3 "${RDO_ROOT}/scripts/render_dashboard.py" --run-id smoke-run >/tmp/rdo-dashboard-path.txt
dashboard_path="$(cat /tmp/rdo-dashboard-path.txt)"
test -f "${dashboard_path}"
grep -q "Run Dashboard" "${dashboard_path}"
grep -q "smoke worker completed" "${dashboard_path}"
