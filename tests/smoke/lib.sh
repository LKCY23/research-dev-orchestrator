#!/usr/bin/env bash

set -euo pipefail

RDO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RDO_KEEP_SMOKE_REPOS="${RDO_KEEP_SMOKE_REPOS:-1}"
RDO_SMOKE_REGISTRY="${RDO_SMOKE_REGISTRY:-$(mktemp -t rdo-smoke-repos.XXXXXX)}"
export RDO_KEEP_SMOKE_REPOS
export RDO_SMOKE_REGISTRY
export RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE=1

cleanup_smoke_repos() {
  if [[ "${RDO_KEEP_SMOKE_REPOS}" != "0" || ! -f "${RDO_SMOKE_REGISTRY}" ]]; then
    return 0
  fi
  while IFS= read -r repo; do
    if [[ -n "${repo}" && -d "${repo}" ]]; then
      rm -rf "${repo}"
    fi
  done < "${RDO_SMOKE_REGISTRY}"
  rm -f "${RDO_SMOKE_REGISTRY}"
}

trap 'code=$?; cleanup_smoke_repos; exit "${code}"' EXIT

setup_smoke_repo() {
  local base="${1:-}"
  if [[ -z "${base}" ]]; then
    base="$(mktemp -d)"
  else
    mkdir -p "${base}"
  fi
  printf '%s\n' "${base}" >> "${RDO_SMOKE_REGISTRY}"
  cd "${base}"
  git init -b main >/dev/null
  git config user.email smoke@example.com
  git config user.name "Smoke Test"
  printf 'hello\n' > file.txt
  git add file.txt
  git commit -m init >/dev/null
  printf '%s\n' "${base}"
}

init_raw_run_and_task() {
  local run_id="$1"
  local task_id="$2"
  local goal="${3:-smoke}"
  python3 "${RDO_ROOT}/scripts/init_run.py" \
    --run-id "${run_id}" \
    --project-slug smoke \
    --objective smoke \
    --target-branch main >/dev/null
  python3 "${RDO_ROOT}/scripts/create_task.py" \
    --run-id "${run_id}" \
    --task-id "${task_id}" \
    --goal "${goal}" \
    --allowed-paths file.txt >/dev/null
}

init_run_and_task() {
  local run_id="$1"
  local task_id="$2"
  local goal="${3:-smoke}"
  local backend_id="${4:-claude-code}"
  init_raw_run_and_task "${run_id}" "${task_id}" "${goal}"
  seed_approved_strategy "${run_id}" "${task_id}" "${backend_id}"
}

seed_approved_strategy() {
  local run_id="$1"
  local task_id="$2"
  local backend_id="${3:-claude-code}"
  local executor_mode="${4:-primary_worker}"
  PYTHONPATH="${RDO_ROOT}/scripts" python3 - "${run_id}" "${task_id}" "${backend_id}" "${executor_mode}" <<'PY'
import json
import sys
from pathlib import Path

from protocol import utc_now, write_json
from strategy import review_strategy, submit_strategy

run_id, task_id, backend_id, executor_mode = sys.argv[1:]
if executor_mode not in {"primary_worker", "native_subagents"}:
    raise SystemExit(f"unsupported smoke executor mode: {executor_mode}")
native = executor_mode == "native_subagents"
task = Path.cwd() / ".agent-collab" / "runs" / run_id / "tasks" / task_id
strategy = {
    "schema_version": 2,
    "backend_id": backend_id,
    "strategy_id": f"{task_id}-S001",
    "task_id": task_id,
    "revision": 1,
    "supersedes": None,
    "objective": "Run the smoke-test worker",
    "global_budget": {
        "wall_seconds": 120,
        "max_workflows": 1,
        "max_workflow_instances": 1,
        "max_parallel_workflows": 1,
        "max_subagents": 1,
        "max_parallel_subagents": 1,
    },
    "workflows": [{
        "workflow_id": "WF-implement",
        "kind": "implementation",
        "purpose": "Exercise execution dispatch",
        "depends_on": [],
        "required": True,
        "executor": {
            "mode": executor_mode,
            "write_access": True,
            "max_agents": 1 if native else 0,
            "max_parallel": 1 if native else 0,
            "allowed_paths": ["file.txt"],
        },
        "budget": {"wall_seconds": 120, "command_seconds": 30, "max_enumerated_cases": 10, "max_instances": 1},
        "completion": {"evidence": "worker handoff"},
        "on_timeout": "block",
    }],
    "runtime_change_policy": {
        "allow_new_instances_of_approved_workflows": True,
        "require_revision_for_new_workflow_kind": True,
        "require_revision_for_budget_increase": True,
        "allow_unbounded_search": False,
    },
    "completion_gate": {
        "required_workflows_complete": False,
        "acceptance_commands_pass": False,
        "optional_workflows_may_timeout": True,
    },
}
submit_strategy(task, strategy)
review_strategy(task, 1, decision="approved", reviewer="smoke-fixture")
status_path = task / "STATUS.json"
status = json.loads(status_path.read_text(encoding="utf-8"))
first = utc_now()
second = utc_now()
status.update(
    previous_state="planning",
    state="strategy_review",
    owner="dispatch",
    updated_at=second,
)
status["state_history"] = [
    {"from": "pending", "to": "planning", "actor": "dispatch", "at": first},
    {"from": "planning", "to": "strategy_review", "actor": "dispatch", "at": second},
]
write_json(status_path, status)
PY
}

make_review_worker() {
  local path="$1"
  cat > "${path}" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
prompt="$(mktemp)"
cat > "${prompt}"
EVIDENCE_PATH="$(awk -F': ' '/^- EVIDENCE_PATH:/ {print $2}' "${prompt}")"
HANDOFF_PATH="$(awk -F': ' '/^- HANDOFF_PATH:/ {print $2}' "${prompt}")"
HANDOFF_JSON_PATH="$(awk -F': ' '/^- HANDOFF_JSON_PATH:/ {print $2}' "${prompt}")"
printf '# Evidence\n\n## Commands Run\n- smoke\n\n## Tests Passed\n- yes\n' > "${EVIDENCE_PATH}"
printf '# Handoff\n\n## What Changed\n- smoke worker completed\n' > "${HANDOFF_PATH}"
if [[ -n "${HANDOFF_JSON_PATH}" ]]; then
  cat > "${HANDOFF_JSON_PATH}" <<'JSON'
{
  "_template": false,
  "requested_state": "review",
  "summary": "smoke worker completed",
  "commands_run": ["smoke"],
  "files_changed": ["file.txt"],
  "known_limitations": [],
  "needs_coordinator": false,
  "blocker_type": "",
  "blocking_reason": ""
}
JSON
fi
SH
  chmod +x "${path}"
}

make_verified_worker() {
  local path="$1"
  cat > "${path}" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
prompt="$(mktemp)"
cat > "${prompt}"
EVIDENCE_PATH="$(awk -F': ' '/^- EVIDENCE_PATH:/ {print $2}' "${prompt}")"
HANDOFF_PATH="$(awk -F': ' '/^- HANDOFF_PATH:/ {print $2}' "${prompt}")"
HANDOFF_JSON_PATH="$(awk -F': ' '/^- HANDOFF_JSON_PATH:/ {print $2}' "${prompt}")"
printf '# Evidence\n\n## Commands Run\n- smoke\n\n## Tests Passed\n- yes\n' > "${EVIDENCE_PATH}"
printf '# Handoff\n\n## Summary\n- direct worker completed and self-reviewed\n' > "${HANDOFF_PATH}"
cat > "${HANDOFF_JSON_PATH}" <<'JSON'
{
  "_template": false,
  "requested_state": "verified",
  "summary": "direct worker completed and self-reviewed",
  "commands_run": ["smoke"],
  "files_changed": ["file.txt"],
  "known_limitations": [],
  "self_review": {
    "acceptance_checked": true,
    "changed_paths_checked": true,
    "tests_passed": true,
    "diff_check_passed": true,
    "findings": [],
    "fixes_applied": [],
    "passed": true
  },
  "needs_coordinator": false,
  "blocker_type": "",
  "blocking_reason": ""
}
JSON
SH
  chmod +x "${path}"
}

make_blocked_worker() {
  local path="$1"
  local blocker_type="${2:-needs_coordinator}"
  local reason="${3:-smoke blocker}"
  cat > "${path}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
EVIDENCE_PATH="\$(awk -F': ' '/^- EVIDENCE_PATH:/ {print \$2}' "\${prompt}")"
HANDOFF_PATH="\$(awk -F': ' '/^- HANDOFF_PATH:/ {print \$2}' "\${prompt}")"
HANDOFF_JSON_PATH="\$(awk -F': ' '/^- HANDOFF_JSON_PATH:/ {print \$2}' "\${prompt}")"
printf '# Evidence\n\n## Commands Run\n- smoke blocked\n\n## Tests Passed\n- no\n' > "\${EVIDENCE_PATH}"
printf '# Handoff\n\n## What Failed\n- ${reason}\n\n## Decision Needed\n- coordinator triage\n' > "\${HANDOFF_PATH}"
cat > "\${HANDOFF_JSON_PATH}" <<'JSON'
{
  "_template": false,
  "requested_state": "blocked",
  "summary": "smoke worker blocked",
  "commands_run": ["smoke blocked"],
  "files_changed": [],
  "known_limitations": ["blocked"],
  "needs_coordinator": true,
  "blocker_type": "${blocker_type}",
  "blocking_reason": "${reason}"
}
JSON
SH
  chmod +x "${path}"
}

make_review_exit1_worker() {
  local path="$1"
  make_review_worker "${path}"
  {
    echo
    echo "exit 1"
  } >> "${path}"
}

make_sleep_worker() {
  local path="$1"
  local seconds="${2:-2}"
  cat > "${path}" <<SH
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
sleep ${seconds}
SH
  chmod +x "${path}"
}

make_persistent_handoff_worker() {
  local path="$1"
  cat > "${path}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
TASK_DIR="\$(awk -F': ' '/^- TASK_DIR:/ {print \$2}' "\${prompt}")"
python3 "${RDO_ROOT}/scripts/rdo.py" handoff \
  --task-dir "\${TASK_DIR}" \
  --state review \
  --summary "persistent interactive worker completed" \
  --command "smoke" \
  --file "file.txt"
sleep 30
SH
  chmod +x "${path}"
}

collect_json() {
  local run_id="$1"
  local output="$2"
  if python3 "${RDO_ROOT}/scripts/collect_status.py" --run-id "${run_id}" --json > "${output}"; then
    return 0
  fi
  return "$?"
}

assert_json_expr() {
  local json_path="$1"
  local expr="$2"
  python3 - "${json_path}" "${expr}" <<'PY'
import json
import sys

path, expr = sys.argv[1:3]
payload = json.load(open(path, encoding="utf-8"))
if not eval(expr, {"payload": payload}):
    raise SystemExit(f"assertion failed: {expr}\n{json.dumps(payload, indent=2)}")
PY
}
