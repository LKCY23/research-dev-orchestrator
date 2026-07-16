#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
fake_bin="${repo}/fake-bin"
mkdir -p "${fake_bin}"
cat > "${fake_bin}/codex" <<'PY'
#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("fake-codex 1.0")
    raise SystemExit(0)
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("fake Codex help")
    raise SystemExit(0)

print(json.dumps({
    "type": "thread.started",
    "thread_id": "11111111-1111-1111-1111-111111111111",
}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({
    "type": "error",
    "message": (
        "The 'gpt-5.6-lune' model is not supported when using Codex "
        "with a ChatGPT account."
    ),
}), flush=True)
print(json.dumps({
    "type": "turn.failed",
    "error": {"message": "model unavailable"},
}), flush=True)
raise SystemExit(1)
PY
chmod +x "${fake_bin}/codex"

init_run_and_task smoke-run T001-codex-model model codex
set +e
PATH="${fake_bin}:${PATH}" RDO_WORKER_BACKEND=codex \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-codex-model \
  > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
rc=$?
set -e
[[ "${rc}" -eq 4 ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-codex-model")
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
attempt_dir = task / "attempts" / status["current_attempt_id"]
attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text(encoding="utf-8"))
startup = json.loads(
    (attempt_dir / "runtime" / "STARTUP.json").read_text(encoding="utf-8")
)
assert startup["state"] == "worker_startup_failed", startup
assert startup["failure"]["code"] == "model_unavailable", startup
assert startup["failure_detected_after_start_event"] is True, startup
assert startup["worker_progress_evidence"] is None, startup
assert status["state"] == "blocked", status
assert status["blocker_type"] == "environment", status
assert attempt["outcome"] == "startup_failed", attempt
assert attempt["startup_failure"]["code"] == "model_unavailable", attempt
assert not (task / ".dispatch-lock").exists()
PY

python3 "${RDO_ROOT}/scripts/collect_status.py" \
  --run-id smoke-run --json > "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
