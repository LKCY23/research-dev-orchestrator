#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"
python3 "${RDO_ROOT}/scripts/init_run.py" \
  --run-id smoke-run \
  --project-slug smoke \
  --objective smoke \
  --target-branch main >/dev/null

python3 - <<'PY'
import json
from pathlib import Path

path = Path(".agent-collab/runs/smoke-run/RUN.json")
payload = json.loads(path.read_text(encoding="utf-8"))
payload["protocol_version"] = "research-dev-orchestrator/v0.0"
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

collect_json smoke-run "${repo}/status.json"
assert_json_expr "${repo}/status.json" "payload['valid'] is True"
assert_json_expr "${repo}/status.json" "'RUN.json protocol_version' in '\\n'.join(payload['protocol_warnings'])"
