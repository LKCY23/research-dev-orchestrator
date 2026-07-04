#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_run_and_task smoke-run T001-config config
cat > .agent-collab/rdo.toml <<'TOML'
[status]
stale_lock_hours = 0.001
stale_created_minutes = 10.0
TOML

task=".agent-collab/runs/smoke-run/tasks/T001-config"
printf 'attempt_id: none\n' > "${task}/LOCK"
python3 - <<'PY'
import os
import time
from pathlib import Path

path = Path(".agent-collab/runs/smoke-run/tasks/T001-config/LOCK")
old = time.time() - 30
os.utime(path, (old, old))
PY

collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['stale_locks']"
