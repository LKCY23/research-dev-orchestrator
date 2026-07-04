#!/usr/bin/env bash

set -euo pipefail

RDO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

setup_smoke_repo() {
  local base="${1:-}"
  if [[ -z "${base}" ]]; then
    base="$(mktemp -d)"
  else
    mkdir -p "${base}"
  fi
  cd "${base}"
  git init -b main >/dev/null
  git config user.email smoke@example.com
  git config user.name "Smoke Test"
  printf 'hello\n' > file.txt
  git add file.txt
  git commit -m init >/dev/null
  printf '%s\n' "${base}"
}

init_run_and_task() {
  local run_id="$1"
  local task_id="$2"
  local goal="${3:-smoke}"
  python3 "${RDO_ROOT}/scripts/init_run.py" \
    --run-id "${run_id}" \
    --project-slug smoke \
    --objective smoke \
    --target-branch main >/dev/null
  python3 "${RDO_ROOT}/scripts/create_task.py" \
    --run-id "${run_id}" \
    --task-id "${task_id}" \
    --goal "${goal}" \
    --allowed-paths file.txt >/dev/null
}

make_review_worker() {
  local path="$1"
  cat > "${path}" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
prompt="$(mktemp)"
cat > "${prompt}"
STATUS_PATH="$(awk -F': ' '/^- STATUS_PATH:/ {print $2}' "${prompt}")"
EVIDENCE_PATH="$(awk -F': ' '/^- EVIDENCE_PATH:/ {print $2}' "${prompt}")"
HANDOFF_PATH="$(awk -F': ' '/^- HANDOFF_PATH:/ {print $2}' "${prompt}")"
printf '# Evidence\n\n## Commands Run\n- smoke\n\n## Tests Passed\n- yes\n' > "${EVIDENCE_PATH}"
printf '# Handoff\n\n## What Changed\n- smoke worker completed\n' > "${HANDOFF_PATH}"
python3 - "${STATUS_PATH}" <<'PY'
import json
import sys
from datetime import datetime, timezone

path = sys.argv[1]
status = json.load(open(path, encoding="utf-8"))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
old = status["state"]
status["previous_state"] = old
status["state"] = "review"
status["updated_at"] = now
status["owner"] = "claude-code"
status.setdefault("state_history", []).append({"from": old, "to": "review", "actor": "claude-code", "at": now})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2)
    handle.write("\n")
PY
SH
  chmod +x "${path}"
}

make_review_exit1_worker() {
  local path="$1"
  make_review_worker "${path}"
  {
    echo
    echo "exit 1"
  } >> "${path}"
}

make_sleep_worker() {
  local path="$1"
  local seconds="${2:-2}"
  cat > "${path}" <<SH
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
sleep ${seconds}
SH
  chmod +x "${path}"
}

collect_json() {
  local run_id="$1"
  local output="$2"
  if python3 "${RDO_ROOT}/scripts/collect_status.py" --run-id "${run_id}" --json > "${output}"; then
    return 0
  fi
  return "$?"
}

assert_json_expr() {
  local json_path="$1"
  local expr="$2"
  python3 - "${json_path}" "${expr}" <<'PY'
import json
import sys

path, expr = sys.argv[1:3]
payload = json.load(open(path, encoding="utf-8"))
if not eval(expr, {"payload": payload}):
    raise SystemExit(f"assertion failed: {expr}\n{json.dumps(payload, indent=2)}")
PY
}
