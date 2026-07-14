#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

python3 - "${RDO_ROOT}" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
import protocol  # noqa: E402

constants = (root / "references" / "protocol-constants.md").read_text(encoding="utf-8")
fsm = json.loads((root / "references" / "state-machine.json").read_text(encoding="utf-8"))

for required in sorted(protocol.BLOCKER_TYPES | protocol.RUNTIME_BACKENDS | protocol.ATTEMPT_STATES):
    assert required in constants, required

doc_task_states = set(
    re.findall(
        r"^(pending|planning|strategy_review|running|blocked|verified|review|changes_requested|approved|merged|failed)$",
        constants,
        re.M,
    )
)
assert protocol.TASK_STATES == set(fsm["states"]) == doc_task_states, (protocol.TASK_STATES, fsm["states"], doc_task_states)

for event in protocol.CORE_EVENTS:
    assert event in constants, event
PY
