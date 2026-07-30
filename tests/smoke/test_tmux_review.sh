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
attempt_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current_attempt_id"])' "${repo}/.agent-collab/runs/smoke-run/tasks/T001-tmux/STATUS.json")"
attempt_dir="${repo}/.agent-collab/runs/smoke-run/tasks/T001-tmux/attempts/${attempt_id}"
session_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session_id"])' "${attempt_dir}/runtime/TMUX_SESSION.json")"
if tmux has-session -t "${session_id}" 2>/dev/null; then
  echo "default tmux dispatch leaked ${session_id}" >&2
  exit 1
fi
assert_json_expr "${attempt_dir}/ATTEMPT.json" "payload['runtime']['tmux_cleanup']['policy'] == 'cleanup_on_exit' and payload['runtime']['tmux_cleanup']['status'] in {'killed', 'already_absent'}"

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
assert_json_expr "${attempt_dir}/runtime/HANDOFF_READY.json" "payload['attempt_id'] == '${attempt_id}' and payload['requested_state'] == 'review'"
assert_json_expr "${attempt_dir}/supervisor-result.json" "payload['completion_requested'] is True"
assert_json_expr "${attempt_dir}/runtime/TMUX_SESSION.json" "payload['run_id'] == 'smoke-persistent' and payload['task_id'] == 'T002-tmux-persistent' and payload['attempt_id'] == '${attempt_id}' and payload['session_id'].startswith(chr(36))"
session_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session_id"])' "${attempt_dir}/runtime/TMUX_SESSION.json")"
if tmux has-session -t "${session_id}" 2>/dev/null; then
  echo "persistent-worker tmux dispatch leaked ${session_id}" >&2
  exit 1
fi
assert_json_expr "${attempt_dir}/ATTEMPT.json" "payload['runtime']['tmux_cleanup']['policy'] == 'cleanup_on_exit' and payload['runtime']['tmux_cleanup']['status'] in {'killed', 'already_absent'}"

repo_retained="$(setup_smoke_repo)"
cd "${repo_retained}"
retained_worker="${repo_retained}/worker-retained-review.sh"
make_review_worker "${retained_worker}"
init_run_and_task smoke-retained T003-tmux-retained tmux
RDO_WORKER_BACKEND=tmux RDO_IO_MODE=human RDO_TMUX_KEEP_SESSION=1 \
  CLAUDE_CODE_CMD="${retained_worker}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-retained T003-tmux-retained
attempt_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current_attempt_id"])' "${repo_retained}/.agent-collab/runs/smoke-retained/tasks/T003-tmux-retained/STATUS.json")"
attempt_dir="${repo_retained}/.agent-collab/runs/smoke-retained/tasks/T003-tmux-retained/attempts/${attempt_id}"
receipt="${attempt_dir}/runtime/TMUX_SESSION.json"
register_smoke_tmux_receipt "${receipt}"
session_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session_id"])' "${receipt}")"
tmux has-session -t "${session_id}"
assert_json_expr "${attempt_dir}/ATTEMPT.json" "payload['runtime']['tmux_cleanup']['policy'] == 'retain' and payload['runtime']['tmux_cleanup']['status'] == 'retained_by_policy'"
cleanup_smoke_tmux_sessions
if tmux has-session -t "${session_id}" 2>/dev/null; then
  echo "retained tmux teardown failed for ${session_id}" >&2
  exit 1
fi
