#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_raw_run_and_task codex-governance-run T001-codex-governance governance
seed_approved_strategy codex-governance-run T001-codex-governance codex native_subagents
task="${repo}/.agent-collab/runs/codex-governance-run/tasks/T001-codex-governance"
python3 - "${repo}/.agent-collab/rdo.toml" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
head, marker, codex = text.rpartition("[backends.codex]")
assert marker and "enforce_spawn_limit = false" in codex
codex = codex.replace("enforce_spawn_limit = false", "enforce_spawn_limit = true", 1)
text = head + marker + codex
path.write_text(text, encoding="utf-8")
PY

fake_bin="${repo}/fake-bin"
mkdir -p "${fake_bin}"
cat > "${fake_bin}/codex" <<'PY'
#!/usr/bin/env python3
import json
import sys
import time

if "--version" in sys.argv:
    print("fake-codex 1.0")
    raise SystemExit(0)
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("fake Codex help")
    raise SystemExit(0)

print(json.dumps({"type": "thread.started", "thread_id": "root"}), flush=True)
for index in (1, 2):
    agent_id = f"agent-{index}"
    print(json.dumps({
        "type": "item.started",
        "item": {
            "id": f"spawn-{index}",
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "root",
            "receiver_thread_ids": [agent_id],
            "agents_states": {agent_id: {"status": "running", "message": None}},
            "status": "in_progress",
        },
    }), flush=True)
time.sleep(30)
PY
chmod +x "${fake_bin}/codex"

set +e
PATH="${fake_bin}:${PATH}" RDO_WORKER_BACKEND=codex \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" codex-governance-run T001-codex-governance \
  >"${repo}/dispatch.out" 2>"${repo}/dispatch.err"
code=$?
set -e

test "${code}" -ne 0
attempt_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current_attempt_id"])' "${task}/STATUS.json")"
attempt="${task}/attempts/${attempt_id}"
python3 - "${attempt}" <<'PY'
import json
import sys
from pathlib import Path

attempt = Path(sys.argv[1])
metadata = json.loads((attempt / "ATTEMPT.json").read_text(encoding="utf-8"))
result = json.loads((attempt / "supervisor-result.json").read_text(encoding="utf-8"))
state = json.loads((attempt / "runtime" / "AGENTS.json").read_text(encoding="utf-8"))
violations = [json.loads(line) for line in (attempt / "runtime" / "VIOLATIONS.ndjson").read_text().splitlines()]
command = metadata["runtime"]["command"]
assert "codex_stream_monitor.py" in command, command
assert "features.multi_agent=true" in command, command
assert "features.enable_fanout=false" in command, command
assert result["exit_code"] == 125, result
assert state["total_requests"] == 2, state
assert len(violations) == 1 and violations[0]["hard"] is True, violations
PY
test ! -d "${task}/.dispatch-lock"
