#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_raw_run_and_task strategy-run T001-strategy lifecycle

planner="${repo}/planner.py"
cat > "${planner}" <<PY
#!/usr/bin/env python3
import json, re, subprocess, sys
from pathlib import Path

prompt = sys.stdin.read()
task = Path(re.search(r"^- TASK_DIR: (.+)$", prompt, re.M).group(1))
attempt = Path(re.search(r"^- ATTEMPT_DIR: (.+)$", prompt, re.M).group(1))
task_id = json.loads((task / "STATUS.json").read_text())["task_id"]
payload = {
    "schema_version": 2,
    "backend_id": "claude-code",
    "strategy_id": f"{task_id}-S001",
    "task_id": task_id,
    "revision": 1,
    "supersedes": None,
    "objective": "Run one bounded verification workflow",
    "global_budget": {"wall_seconds": 60, "max_workflows": 1, "max_workflow_instances": 1, "max_parallel_workflows": 1, "max_subagents": 1, "max_parallel_subagents": 1},
    "workflows": [{
        "workflow_id": "WF-verify",
        "kind": "verification",
        "purpose": "Run deterministic acceptance command",
        "depends_on": [],
        "required": True,
        "executor": {"mode": "primary_worker", "write_access": False, "max_agents": 0, "max_parallel": 0, "allowed_paths": ["file.txt"]},
        "budget": {"wall_seconds": 30, "command_seconds": 10, "max_enumerated_cases": 1, "max_instances": 1},
        "completion": {"evidence": "true exits zero"},
        "on_timeout": "block"
    }],
    "runtime_change_policy": {"allow_new_instances_of_approved_workflows": True, "require_revision_for_new_workflow_kind": True, "require_revision_for_budget_increase": True, "allow_unbounded_search": False},
    "completion_gate": {"required_workflows_complete": True, "acceptance_commands_pass": True, "optional_workflows_may_timeout": False}
}
candidate = attempt / "strategy-candidate.json"
candidate.write_text(json.dumps(payload))
subprocess.run([sys.executable, "${RDO_ROOT}/scripts/rdo.py", "strategy", "submit", "--task-dir", str(task), "--file", str(candidate)], check=True)
PY
chmod +x "${planner}"

CLAUDE_CODE_CMD="${planner}" "${RDO_ROOT}/scripts/dispatch_claude.sh" strategy-run T001-strategy
task="${repo}/.agent-collab/runs/strategy-run/tasks/T001-strategy"
python3 - "${task}" <<'PY'
import json, sys
from pathlib import Path
task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text())
assert status["state"] == "strategy_review", status
assert status["state_history"][-1]["from"] == "planning"
assert status["state_history"][-1]["to"] == "strategy_review"
attempt = task / "attempts" / status["current_attempt_id"]
ready = json.loads((attempt / "runtime" / "HANDOFF_READY.json").read_text())
handoff = json.loads((attempt / "HANDOFF.json").read_text())
evidence = json.loads((attempt / "EVIDENCE.json").read_text())
assert ready["attempt_id"] == status["current_attempt_id"]
assert ready["requested_state"] == "strategy_review"
assert handoff["requested_state"] == "strategy_review"
assert any(item["ref"] == "runtime/STRATEGY_SUBMISSION.json" for item in evidence["artifacts"])
assert not (task / "HANDOFF.json").exists()
PY

python3 "${RDO_ROOT}/scripts/rdo.py" strategy approve --task-dir "${task}" --revision 1 --reviewer smoke >/dev/null

executor="${repo}/executor.py"
cat > "${executor}" <<PY
#!/usr/bin/env python3
import re, subprocess, sys
from pathlib import Path

prompt = sys.stdin.read()
task = Path(re.search(r"^- TASK_DIR: (.+)$", prompt, re.M).group(1))
attempt = Path(re.search(r"^- ATTEMPT_DIR: (.+)$", prompt, re.M).group(1))
rdo = [sys.executable, "${RDO_ROOT}/scripts/rdo.py"]
subprocess.run(rdo + ["workflow", "start", "--attempt-dir", str(attempt), "--workflow-id", "WF-verify", "--instance-id", "WF-verify-I001"], check=True)
subprocess.run(rdo + ["check", "--attempt-dir", str(attempt), "--check-id", "smoke", "--workflow-id", "WF-verify", "--instance-id", "WF-verify-I001"], check=True)
subprocess.run(rdo + ["workflow", "complete", "--attempt-dir", str(attempt), "--workflow-id", "WF-verify", "--instance-id", "WF-verify-I001"], check=True)
subprocess.run(rdo + ["finalize", "--attempt-dir", str(attempt), "--state", "review", "--summary", "strategy lifecycle complete"], check=True)
PY
chmod +x "${executor}"

CLAUDE_CODE_CMD="${executor}" "${RDO_ROOT}/scripts/dispatch_claude.sh" strategy-run T001-strategy
attempt_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current_attempt_id"])' "${task}/STATUS.json")"
test -s "${task}/attempts/${attempt_id}/runtime/BACKEND_PROFILE.json"
python3 - "${task}/attempts/${attempt_id}" <<'PY'
import json, sys
from pathlib import Path
attempt = Path(sys.argv[1])
metadata = json.loads((attempt / "ATTEMPT.json").read_text())
profile = json.loads((attempt / "runtime" / "BACKEND_PROFILE.json").read_text())
assert metadata["backend_profile_sha256"] == profile["profile_sha256"]
assert profile["backend_id"] == "claude-code"
ready = json.loads((attempt / "runtime" / "HANDOFF_READY.json").read_text())
handoff = json.loads((attempt / "HANDOFF.json").read_text())
evidence = json.loads((attempt / "EVIDENCE.json").read_text())
assert ready["attempt_id"] == metadata["attempt_id"]
assert ready["requested_state"] == "review"
assert handoff["requested_state"] == "review"
assert [item["check_id"] for item in evidence["command_records"]] == ["smoke"]
assert evidence["command_records"][0]["argv"] == ["/usr/bin/true"]
PY
collect_json strategy-run "${repo}/strategy-status.json"
assert_json_expr "${repo}/strategy-status.json" "payload['valid'] is True"
assert_json_expr "${repo}/strategy-status.json" "payload['tasks'][0]['state'] == 'review'"
python3 "${RDO_ROOT}/scripts/protocol_cli.py" validate-handoff \
  --status-path "${task}/STATUS.json" \
  --attempt-id "${attempt_id}" \
  --task-dir "${task}" \
  --attempt-path "${task}/attempts/${attempt_id}/ATTEMPT.json" \
  --supervisor-result "${task}/attempts/${attempt_id}/supervisor-result.json" \
  --exit-code-raw 0
