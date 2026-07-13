#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

repo="$(setup_smoke_repo)"
cd "${repo}"

index=1
for combination in "plain human" "tmux machine"; do
  read -r runtime io <<< "${combination}"
  task_id="T00${index}-${runtime}-${io}"
  run_id="smoke-${runtime}-${io}"
  init_raw_run_and_task "${run_id}" "${task_id}" matrix
  set +e
  "${RDO_ROOT}/scripts/dispatch_agent.sh" "${run_id}" "${task_id}" \
    --worker claude-code --runtime "${runtime}" --io "${io}" \
    > "${repo}/${task_id}.out" 2> "${repo}/${task_id}.err"
  rc=$?
  set -e
  [[ "${rc}" -eq 2 ]]
  grep -q "supported combinations: plain + machine; tmux + human" "${repo}/${task_id}.err"
  index=$((index + 1))
done

python3 - <<'PY'
import json
from pathlib import Path

runs = Path(".agent-collab/runs")
for task in (run / "tasks" for run in runs.iterdir()):
    task = next(task.iterdir())
    status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
    assert status["state"] == "pending", status
    assert not (task / ".dispatch-lock").exists()
    assert not (task / "LOCK").exists()
    assert not list((task / "attempts").iterdir())
PY
