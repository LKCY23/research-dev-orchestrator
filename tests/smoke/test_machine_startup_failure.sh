#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
fake_bin="${repo}/fake-bin"
mkdir -p "${fake_bin}"
cat > "${fake_bin}/claude" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "fake-claude 1.0"
  exit 0
fi
if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
  echo '{"loggedIn":true}'
  exit 0
fi
exit 0
SH
chmod +x "${fake_bin}/claude"

init_run_and_task smoke-run T001-startup startup
set +e
PATH="${fake_bin}:${PATH}" RDO_STARTUP_TIMEOUT_SECONDS=1 \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-startup \
  > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
rc=$?
set -e
[[ "${rc}" -eq 4 ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-startup")
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text(encoding="utf-8"))
startup = json.loads((attempt_dir / "runtime/STARTUP.json").read_text(encoding="utf-8"))
assert startup["state"] == "worker_startup_failed", startup
assert status["state"] == "blocked", status
assert status["blocker_type"] == "environment", status
assert status["summary"] == "Worker startup failed", status
assert "early_exit" in status["blocking_reason"], status
assert attempt["startup_failure"]["code"] == "early_exit", attempt
assert attempt["outcome"] == "startup_failed", attempt
assert not (task / ".dispatch-lock").exists()
PY

python3 "${RDO_ROOT}/scripts/collect_status.py" --run-id smoke-run --json > "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
