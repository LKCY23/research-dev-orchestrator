#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-tmux tmux
RDO_WORKER_BACKEND=tmux CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-tmux
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['state'] == 'review'"
