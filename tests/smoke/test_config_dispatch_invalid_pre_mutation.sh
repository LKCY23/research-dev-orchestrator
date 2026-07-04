#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
init_run_and_task smoke-run T001-invalid-config invalid
cat > .agent-collab/rdo.toml <<'TOML'
[runtime]
backend = "daemon"
TOML

set +e
"${RDO_ROOT}/scripts/dispatch_claude.sh" smoke-run T001-invalid-config > "${repo}/dispatch.out" 2> "${repo}/dispatch.err"
dispatch_code="$?"
set -e

[[ "${dispatch_code}" != "0" ]]

python3 - <<'PY'
import json
from pathlib import Path

task = Path(".agent-collab/runs/smoke-run/tasks/T001-invalid-config")
status = json.load(open(task / "STATUS.json", encoding="utf-8"))
assert status["state"] == "pending", status
assert not (task / ".dispatch-lock").exists()
assert not (task / "LOCK").exists()
assert not any((task / "attempts").iterdir())
PY
