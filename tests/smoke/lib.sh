#!/usr/bin/env bash

set -euo pipefail

RDO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RDO_KEEP_SMOKE_REPOS="${RDO_KEEP_SMOKE_REPOS:-1}"
RDO_SMOKE_REGISTRY="${RDO_SMOKE_REGISTRY:-$(mktemp -t rdo-smoke-repos.XXXXXX)}"
export RDO_KEEP_SMOKE_REPOS
export RDO_SMOKE_REGISTRY

cleanup_smoke_repos() {
  if [[ "${RDO_KEEP_SMOKE_REPOS}" != "0" || ! -f "${RDO_SMOKE_REGISTRY}" ]]; then
    return 0
  fi
  while IFS= read -r repo; do
    if [[ -n "${repo}" && -d "${repo}" ]]; then
      rm -rf "${repo}"
    fi
  done < "${RDO_SMOKE_REGISTRY}"
  rm -f "${RDO_SMOKE_REGISTRY}"
}

trap 'code=$?; cleanup_smoke_repos; exit "${code}"' EXIT

setup_smoke_repo() {
  local base="${1:-}"
  if [[ -z "${base}" ]]; then
    base="$(mktemp -d)"
  else
    mkdir -p "${base}"
  fi
  printf '%s\n' "${base}" >> "${RDO_SMOKE_REGISTRY}"
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
EVIDENCE_PATH="$(awk -F': ' '/^- EVIDENCE_PATH:/ {print $2}' "${prompt}")"
HANDOFF_PATH="$(awk -F': ' '/^- HANDOFF_PATH:/ {print $2}' "${prompt}")"
HANDOFF_JSON_PATH="$(awk -F': ' '/^- HANDOFF_JSON_PATH:/ {print $2}' "${prompt}")"
printf '# Evidence\n\n## Commands Run\n- smoke\n\n## Tests Passed\n- yes\n' > "${EVIDENCE_PATH}"
printf '# Handoff\n\n## What Changed\n- smoke worker completed\n' > "${HANDOFF_PATH}"
if [[ -n "${HANDOFF_JSON_PATH}" ]]; then
  cat > "${HANDOFF_JSON_PATH}" <<'JSON'
{
  "_template": false,
  "requested_state": "review",
  "summary": "smoke worker completed",
  "commands_run": ["smoke"],
  "files_changed": ["file.txt"],
  "known_limitations": [],
  "needs_coordinator": false,
  "blocker_type": "",
  "blocking_reason": ""
}
JSON
fi
SH
  chmod +x "${path}"
}

make_blocked_worker() {
  local path="$1"
  local blocker_type="${2:-needs_coordinator}"
  local reason="${3:-smoke blocker}"
  cat > "${path}" <<SH
#!/usr/bin/env bash
set -euo pipefail
prompt="\$(mktemp)"
cat > "\${prompt}"
EVIDENCE_PATH="\$(awk -F': ' '/^- EVIDENCE_PATH:/ {print \$2}' "\${prompt}")"
HANDOFF_PATH="\$(awk -F': ' '/^- HANDOFF_PATH:/ {print \$2}' "\${prompt}")"
HANDOFF_JSON_PATH="\$(awk -F': ' '/^- HANDOFF_JSON_PATH:/ {print \$2}' "\${prompt}")"
printf '# Evidence\n\n## Commands Run\n- smoke blocked\n\n## Tests Passed\n- no\n' > "\${EVIDENCE_PATH}"
printf '# Handoff\n\n## What Failed\n- ${reason}\n\n## Decision Needed\n- coordinator triage\n' > "\${HANDOFF_PATH}"
cat > "\${HANDOFF_JSON_PATH}" <<'JSON'
{
  "_template": false,
  "requested_state": "blocked",
  "summary": "smoke worker blocked",
  "commands_run": ["smoke blocked"],
  "files_changed": [],
  "known_limitations": ["blocked"],
  "needs_coordinator": true,
  "blocker_type": "${blocker_type}",
  "blocking_reason": "${reason}"
}
JSON
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
