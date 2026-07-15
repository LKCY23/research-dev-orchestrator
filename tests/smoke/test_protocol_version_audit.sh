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

python3 - "${RDO_ROOT}/VERSION" <<'PY'
import json
import sys
from pathlib import Path

versions = {}
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    key, value = line.split("=", 1)
    versions[key] = value

path = Path(".agent-collab/runs/smoke-run/RUN.json")
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload["package_version"] == versions["PACKAGE_VERSION"]
assert payload["protocol_version"] == versions["PROTOCOL_VERSION"]
payload["protocol_version"] = "research-dev-orchestrator/v0.5"
payload["package_version"] = "0.5.0"
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

collect_json smoke-run "${repo}/legacy-status.json"
assert_json_expr "${repo}/legacy-status.json" "payload['valid'] is True"
assert_json_expr "${repo}/legacy-status.json" "'RUN.json protocol_version' in '\\n'.join(payload['protocol_warnings'])"

python3 - <<'PY'
import json
from pathlib import Path

path = Path(".agent-collab/runs/smoke-run/RUN.json")
payload = json.loads(path.read_text(encoding="utf-8"))
payload["protocol_version"] = "research-dev-orchestrator/v0.0"
payload["package_version"] = "0.0.0"
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

set +e
collect_json smoke-run "${repo}/status.json"
set -e
assert_json_expr "${repo}/status.json" "payload['valid'] is False"
assert_json_expr "${repo}/status.json" "'RUN.json protocol_version' in '\\n'.join(payload['protocol_violations'])"
assert_json_expr "${repo}/status.json" "'RUN.json package_version' in '\\n'.join(payload['protocol_warnings'])"
