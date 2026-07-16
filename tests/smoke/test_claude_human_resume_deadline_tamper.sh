#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not found"; exit 0; }

repo="$(setup_smoke_repo)"
cd "${repo}"
fake_bin="${repo}/fake-bin"
fake_home="${repo}/fake-claude-home"
fallback_sentinel="${repo}/fallback-ran"
session_id="44444444-4444-4444-4444-444444444444"
mkdir -p "${fake_bin}" "${fake_home}/projects/project"
printf '{}\n' > "${fake_home}/projects/project/${session_id}.jsonl"

cat > "${fake_bin}/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  echo "fake-claude 2.1.185"
  exit 0
fi
if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
  echo '{"loggedIn":true}'
  exit 0
fi
if [[ " $* " == *" --help "* ]]; then
  echo "fake Claude help"
  exit 0
fi
if [[ "$*" == *" --resume "* ]]; then
  prompt="${!#}"
  attempt_dir="$(printf '%s\n' "${prompt}" | awk -F': ' '/^- ATTEMPT_DIR:/ {print $2}')"
  python3 - "${attempt_dir}/runtime/DEADLINE.json" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["started_at_epoch"] += 300
payload["execution_deadline_at_epoch"] += 300
for source, target in (
    ("started_at_epoch", "started_at"),
    ("execution_deadline_at_epoch", "execution_deadline_at"),
):
    payload[target] = (
        datetime.fromtimestamp(payload[source], timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
  echo "No conversation found with session ID: 44444444-4444-4444-4444-444444444444"
  exit 1
fi
touch "${FALLBACK_SENTINEL}"
exit 1
SH
freeze_worker_rdo_root "${fake_bin}/claude"
chmod +x "${fake_bin}/claude"

python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id deadline-tamper-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null
python3 "${RDO_ROOT}/scripts/create_task.py" \
  --run-id deadline-tamper-run \
  --task-id T001-deadline-tamper \
  --goal fallback \
  --profile delegated \
  --allowed-paths file.txt >/dev/null
complete_task_contract deadline-tamper-run T001-deadline-tamper fallback

task="${repo}/.agent-collab/runs/deadline-tamper-run/tasks/T001-deadline-tamper"
python3 - "${task}" "${session_id}" <<'PY'
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
session_id = sys.argv[2]
status_path = task / "STATUS.json"
status = json.loads(status_path.read_text())
status.update(
    state="blocked",
    previous_state="running",
    owner="worker",
    current_attempt_id="A000-claude-prior",
    needs_coordinator=True,
    blocker_type="needs_coordinator",
    blocking_reason="retry fixture",
    assigned_worker={
        "worker_id": "W-stable-human",
        "backend_id": "claude-code",
        "agent": "claude-code",
        "agent_name": "claude-worker",
        "backend_session_id": session_id,
        "session_id": session_id,
        "first_attempt_id": "A000-claude-prior",
        "latest_attempt_id": "A000-claude-prior",
        "role": "worker",
    },
)
status["state_history"] = [
    {"from": "pending", "to": "running", "actor": "dispatch", "at": "2026-07-16T00:00:00Z"},
    {"from": "running", "to": "blocked", "actor": "dispatch", "at": "2026-07-16T00:01:00Z"},
]
status_path.write_text(json.dumps(status, indent=2) + "\n")
PY

set +e
PATH="${fake_bin}:${PATH}" \
CLAUDE_CONFIG_DIR="${fake_home}" \
FALLBACK_SENTINEL="${fallback_sentinel}" \
RDO_RUNTIME_BACKEND=tmux \
RDO_IO_MODE=human \
RDO_TMUX_WAIT_TIMEOUT_SECONDS=15 \
  "${RDO_ROOT}/scripts/dispatch_agent.sh" \
  deadline-tamper-run T001-deadline-tamper >/dev/null 2>&1
dispatch_code="$?"
set -e

[[ "${dispatch_code}" -ne 0 ]]
test ! -e "${fallback_sentinel}"

python3 - "${task}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

task = Path(sys.argv[1])
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
attempt_dir = task / "attempts" / status["current_attempt_id"]
result = json.loads((attempt_dir / "supervisor-result.json").read_text(encoding="utf-8"))
current_digest = hashlib.sha256(
    (attempt_dir / "runtime" / "DEADLINE.json").read_bytes()
).hexdigest()
assert result["deadline_sha256"] != current_digest, (result, current_digest)
assert not (attempt_dir / "runtime" / "RESUME_SUPERVISOR_FAILURE.json").exists()
assert status["state"] == "blocked", status
PY
