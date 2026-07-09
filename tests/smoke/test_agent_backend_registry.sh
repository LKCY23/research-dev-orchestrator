#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

python3 "${RDO_ROOT}/scripts/agent_backend_cli.py" validate --backend all >/dev/null
python3 "${RDO_ROOT}/scripts/agent_backend_cli.py" list > /tmp/rdo-backends.$$
grep -qx "claude-code" /tmp/rdo-backends.$$
grep -qx "codex" /tmp/rdo-backends.$$
grep -qx "opencode" /tmp/rdo-backends.$$
grep -qx "kimi-code" /tmp/rdo-backends.$$
rm -f /tmp/rdo-backends.$$

python3 "${RDO_ROOT}/scripts/agent_backend_cli.py" command \
  --backend kimi-code \
  --io-mode machine \
  --permission-mode auto \
  --cwd /tmp/example \
  --prompt "Say OK only." \
  --json > /tmp/rdo-kimi-command.$$

python3 - /tmp/rdo-kimi-command.$$ <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["prompt_transport"] == "arg", payload
assert "kimi" in payload["command"], payload
assert "--output-format" in payload["command"], payload
assert "stream-json" in payload["command"], payload
PY
rm -f /tmp/rdo-kimi-command.$$

python3 "${RDO_ROOT}/scripts/agent_backend_cli.py" command \
  --backend kimi-code \
  --io-mode human \
  --permission-mode auto \
  --cwd /tmp/example \
  --prompt "Say OK only." \
  --json > /tmp/rdo-kimi-human-command.$$

python3 - /tmp/rdo-kimi-human-command.$$ <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["prompt_transport"] == "tmux_send_keys", payload
assert payload["submit_key"] == "C-m", payload
assert payload["post_paste_delay_ms"] == 1000, payload
assert payload["command"] == "kimi --auto", payload
PY
rm -f /tmp/rdo-kimi-human-command.$$
