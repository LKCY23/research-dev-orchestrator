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
cat > .agent-collab/rdo.toml <<'TOML'
[runtime]
backend = "daemon"

[tmux]
wait_timeout_seconds = -1

[unknown]
value = true
TOML

set +e
python3 "${RDO_ROOT}/scripts/config_cli.py" validate > "${repo}/validate.out" 2> "${repo}/validate.err"
validate_code="$?"
collect_json smoke-run "${repo}/status.json"
collect_code="$?"
set -e

[[ "${validate_code}" != "0" ]]
[[ "${collect_code}" != "0" ]]
assert_json_expr "${repo}/status.json" "'config: [runtime].backend must be one of' in '\\n'.join(payload['protocol_violations'])"
assert_json_expr "${repo}/status.json" "'config: unknown section [unknown]' in '\\n'.join(payload['protocol_warnings'])"
