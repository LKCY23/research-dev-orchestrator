#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
make_review_worker "${worker}"

init_run_and_task smoke-run T001-tmux tmux
RDO_WORKER_BACKEND=tmux RDO_IO_MODE=human CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-tmux
collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "payload['tasks'][0]['state'] == 'review'"

repo_persistent="$(setup_smoke_repo)"
cd "${repo_persistent}"
persistent_worker="${repo_persistent}/worker-persistent-review.sh"
make_persistent_handoff_worker "${persistent_worker}"
init_run_and_task smoke-persistent T002-tmux-persistent tmux
started="$(date +%s)"
RDO_WORKER_BACKEND=tmux RDO_IO_MODE=human CLAUDE_CODE_CMD="${persistent_worker}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-persistent T002-tmux-persistent
elapsed="$(( $(date +%s) - started ))"
[[ "${elapsed}" -lt 15 ]] || { echo "completion supervisor did not quiesce the persistent worker" >&2; exit 1; }
collect_json smoke-persistent "${repo_persistent}/status.json"
assert_json_expr "${repo_persistent}/status.json" "payload['valid'] is True"
assert_json_expr "${repo_persistent}/status.json" "payload['tasks'][0]['state'] == 'review'"
attempt_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current_attempt_id"])' "${repo_persistent}/.agent-collab/runs/smoke-persistent/tasks/T002-tmux-persistent/STATUS.json")"
attempt_dir="${repo_persistent}/.agent-collab/runs/smoke-persistent/tasks/T002-tmux-persistent/attempts/${attempt_id}"
assert_json_expr "${attempt_dir}/COMPLETION.json" "payload['attempt_id'] == '${attempt_id}'"
assert_json_expr "${attempt_dir}/supervisor-result.json" "payload['completion_requested'] is True"
