#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_raw_run_and_task resume-run T001-resume semantic-resume
task="${repo}/.agent-collab/runs/resume-run/tasks/T001-resume"
worktree="${repo}/.agent-worktrees/T001-resume"
git worktree add -b agent/T001-resume "${worktree}" HEAD >/dev/null

PYTHONPATH="${RDO_ROOT}/scripts" python3 - "${task}" <<'PY'
import json, sys
from pathlib import Path

from protocol import utc_now, write_json
from strategy import review_strategy, submit_strategy

task = Path(sys.argv[1])
base_workflow = {
    "workflow_id": "WF-old-implementation",
    "kind": "implementation",
    "purpose": "Produce the implementation output",
    "depends_on": [],
    "required": True,
    "executor": {"mode": "primary_worker", "write_access": True, "max_agents": 0, "max_parallel": 0, "allowed_paths": ["file.txt"]},
    "budget": {"wall_seconds": 60, "command_seconds": 10, "max_enumerated_cases": 1, "max_instances": 1},
    "completion": {"evidence": "implementation exists"},
    "on_timeout": "block",
}
common = {
    "schema_version": 2,
    "task_id": "T001-resume",
    "objective": "Resume prior implementation and revalidate acceptance",
    "global_budget": {"wall_seconds": 120, "max_workflows": 2, "max_workflow_instances": 2, "max_parallel_workflows": 1, "max_subagents": 1, "max_parallel_subagents": 1},
    "runtime_change_policy": {"allow_new_instances_of_approved_workflows": True, "require_revision_for_new_workflow_kind": True, "require_revision_for_budget_increase": True, "allow_unbounded_search": False},
    "completion_gate": {"required_workflows_complete": True, "acceptance_commands_pass": True, "optional_workflows_may_timeout": False},
}
v1 = {**common, "backend_id": "codex", "strategy_id": "T001-resume-S001", "revision": 1, "supersedes": None, "workflows": [base_workflow]}
submit_strategy(task, v1)
implementation = {
    **base_workflow,
    "workflow_id": "WF-implementation",
    "resume": {"from_attempt": "A001-codex", "from_workflow": "WF-old-implementation", "mode": "reuse"},
}
acceptance = {
    "workflow_id": "WF-acceptance",
    "kind": "verification",
    "purpose": "Revalidate the current worktree",
    "depends_on": ["WF-implementation"],
    "required": True,
    "executor": {"mode": "primary_worker", "write_access": False, "max_agents": 0, "max_parallel": 0, "allowed_paths": ["file.txt"]},
    "budget": {"wall_seconds": 60, "command_seconds": 10, "max_enumerated_cases": 1, "max_instances": 1},
    "completion": {"evidence": "acceptance passes"},
    "on_timeout": "block",
}
v2 = {**common, "backend_id": "claude-code", "strategy_id": "T001-resume-S002", "revision": 2, "supersedes": v1["strategy_id"], "workflows": [implementation, acceptance]}
submit_strategy(task, v2)
review_strategy(task, 2, decision="approved", reviewer="smoke")

source = task / "attempts" / "A001-codex"
(source / "runtime").mkdir(parents=True)
write_json(source / "ATTEMPT.json", {
    "attempt_id": "A001-codex", "task_id": "T001-resume", "state": "invalid_handoff",
    "phase": "execution", "backend_id": "codex", "strategy_id": v1["strategy_id"],
    "strategy_sha256": "source-strategy", "worker_id": "W-old",
})
(source / "runtime" / "WORKFLOWS.ndjson").write_text(json.dumps({
    "event": "workflow_completed", "workflow_id": "WF-old-implementation", "instance_id": "I001"
}) + "\n")

status_path = task / "STATUS.json"
status = json.loads(status_path.read_text())
now = utc_now()
status.update(
    state="strategy_review", previous_state="planning", current_attempt_id="A001-codex",
    assigned_worker={"worker_id": "W-old", "backend_id": "codex", "agent": "codex", "agent_name": "codex-worker", "session_id": "old-session", "backend_session_id": "old-session", "role": "worker"},
    updated_at=now,
)
status["state_history"] = [
    {"from": "pending", "to": "planning", "actor": "dispatch", "at": now},
    {"from": "planning", "to": "strategy_review", "actor": "dispatch", "at": now},
]
write_json(status_path, status)
PY

python3 "${RDO_ROOT}/scripts/worktree_fingerprint.py" \
  --worktree "${worktree}" \
  --output "${task}/attempts/A001-codex/runtime/worktree-after.json"

worker="${repo}/resume-worker.py"
cat > "${worker}" <<PY
#!/usr/bin/env python3
import json, re, subprocess, sys
from pathlib import Path

prompt = sys.stdin.read()
task = Path(re.search(r"^- TASK_DIR: (.+)$", prompt, re.M).group(1))
attempt = Path(re.search(r"^- ATTEMPT_DIR: (.+)$", prompt, re.M).group(1))
context = json.loads((attempt / "runtime" / "RESUME_CONTEXT.json").read_text())
assert context["carried_forward_workflows"] == ["WF-implementation"], context
assert context["remaining_workflows"] == ["WF-acceptance"], context
rdo = [sys.executable, "${RDO_ROOT}/scripts/rdo.py"]
duplicate = subprocess.run(rdo + ["workflow", "start", "--attempt-dir", str(attempt), "--workflow-id", "WF-implementation", "--instance-id", "duplicate"], capture_output=True, text=True)
assert duplicate.returncode != 0 and "already satisfied" in duplicate.stderr, duplicate
subprocess.run(rdo + ["workflow", "start", "--attempt-dir", str(attempt), "--workflow-id", "WF-acceptance", "--instance-id", "accept-I001"], check=True)
subprocess.run(rdo + ["check", "--attempt-dir", str(attempt), "--check-id", "smoke", "--workflow-id", "WF-acceptance", "--instance-id", "accept-I001"], check=True)
subprocess.run(rdo + ["workflow", "complete", "--attempt-dir", str(attempt), "--workflow-id", "WF-acceptance", "--instance-id", "accept-I001"], check=True)
subprocess.run(rdo + ["finalize", "--attempt-dir", str(attempt), "--state", "review", "--summary", "semantic resume complete"], check=True)
PY
chmod +x "${worker}"

CLAUDE_CODE_CMD="${worker}" "${RDO_ROOT}/scripts/dispatch_claude.sh" resume-run T001-resume

python3 - "${task}" <<'PY'
import json, sys
from pathlib import Path
task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
assert status["state"] == "review", status
attempt = task / "attempts" / status["current_attempt_id"]
metadata = json.loads((attempt / "ATTEMPT.json").read_text())
assert metadata["execution_mode"] == "replace", metadata
records = [json.loads(line) for line in (attempt / "runtime" / "WORKFLOWS.ndjson").read_text().splitlines()]
assert [item["event"] for item in records] == ["workflow_carried_forward", "workflow_started", "workflow_completed"], records
ready = json.loads((attempt / "runtime" / "HANDOFF_READY.json").read_text())
evidence = json.loads((attempt / "EVIDENCE.json").read_text())
assert ready["requested_state"] == "review"
assert [item["check_id"] for item in evidence["command_records"]] == ["smoke"]
PY
