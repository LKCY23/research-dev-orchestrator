# Configuration

Use this reference when changing project-level runtime defaults or interpreting `.agent-collab/rdo.toml`.

Configuration is only for operational defaults. It must not define protocol states, schema fields, event types, blocker types, protocol version, or FSM transitions.

## Layers

```text
protocol truth:
  scripts/protocol.py
  references/state-machine.json
  STATUS / ATTEMPT / EVENTS schemas

operational defaults:
  .agent-collab/rdo.toml
  scripts/config.py

one-off overrides:
  /rdo args
  CLI flags
  environment variables
```

Precedence:

```text
CLI flag or /rdo one-off argument
> environment variable
> .agent-collab/rdo.toml
> built-in defaults
```

## rdo.toml

`init_run.py` creates `.agent-collab/rdo.toml` if it does not exist.

```toml
[worker]
command = "claude"
agent_name = "claude-worker"

[runtime]
backend = "plain" # plain | tmux

[tmux]
session_prefix = "rdo"
keep_session = false
wait_timeout_seconds = 0
exit_code_grace_seconds = 60

[status]
stale_lock_hours = 6.0
stale_created_minutes = 10.0

[task]
branch_prefix = "agent/"
worktree_root = ".agent-worktrees"
```

Do not add `session_id` to this file. Session id is runtime identity and should be passed with `CLAUDE_SESSION_ID` when available.

Do not add `protocol_version`. Protocol version is defined by the installed package in `scripts/protocol.py`, written to `RUN.json`, and audited by `collect_status.py`.

## Environment Overrides

```text
CLAUDE_CODE_CMD
CLAUDE_AGENT_NAME
CLAUDE_SESSION_ID
RDO_WORKER_BACKEND
RDO_TMUX_SESSION_PREFIX
RDO_TMUX_KEEP_SESSION
RDO_TMUX_WAIT_TIMEOUT_SECONDS
RDO_TMUX_EXIT_CODE_GRACE_SECONDS
RDO_STALE_LOCK_HOURS
RDO_STALE_CREATED_MINUTES
RDO_TASK_BRANCH_PREFIX
RDO_WORKTREE_ROOT
```

`worker.command` and `CLAUDE_CODE_CMD` are interpreted by the dispatch shell. Do not put secrets in them; prefer environment variables for credentials.

## Current Script Integration

Python-side scripts consume config first:

```text
collect_status.py
  stale thresholds and tmux exit_code grace

close_session.py
  stale thresholds and tmux exit_code grace through collect_status

create_task.py
  default branch and worktree from task.branch_prefix and task.worktree_root
```

`dispatch_claude.sh` continues to use explicit environment variables in this phase. Use `scripts/config_cli.py export-env` to inspect resolved values before dispatch integration.

When dispatch integration is added, do not use unchecked command substitution:

```bash
eval "$(python scripts/config_cli.py export-env)"
```

Capture output and exit status first, then `eval` only on success. Config errors must stop dispatch before locks, attempts, worktrees, or `STATUS -> running` mutations.

## Diagnostics

Unknown TOML sections or keys are warnings. Invalid values are errors.

`collect_status.py` reports config warnings under `protocol_warnings` and config errors under `protocol_violations`.

`config_cli.py validate` exits nonzero when config has errors.
