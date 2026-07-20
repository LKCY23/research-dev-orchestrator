#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
worker="${repo}/worker-review.sh"
fake_bin="${repo}/fake-bin"
mkdir -p "${fake_bin}"
make_review_worker "${worker}"

cat > "${fake_bin}/tmux" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  new-session)
    echo "failed to connect to tmux socket: operation not permitted" >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
SH
chmod +x "${fake_bin}/tmux"

init_run_and_task smoke-run T001-tmux-preflight tmux-preflight
cat > .agent-collab/rdo.toml <<TOML
[worker]
command = "${worker}"

[runtime]
backend = "tmux"
io_mode = "human"
TOML

set +e
PATH="${fake_bin}:${PATH}" \
  "${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-tmux-preflight \
  > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
dispatch_code="$?"
set -e

[[ "${dispatch_code}" != "0" ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-tmux-preflight")
status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
assert status["state"] == "strategy_review", status
assert not (task / ".dispatch-lock").exists()
assert not (task / "LOCK").exists()
assert not any((task / "attempts").iterdir())

events = [json.loads(line) for line in (task.parent.parent / "EVENTS.ndjson").read_text(encoding="utf-8").splitlines()]
failed = [event for event in events if event["event"] == "dispatch_preflight_failed"]
assert len(failed) == 1, failed
event = failed[0]
assert "attempt_id" not in event, event
assert event["runtime_backend"] == "tmux", event
assert "operation not permitted" in event["detail"], event
PY
