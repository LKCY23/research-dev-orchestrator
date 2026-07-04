#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-plain plain
CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-plain
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['state'] == 'review'"
