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
  coordinator intent arguments
  CLI flags
  environment variables
```

Precedence:

```text
CLI flag or coordinator intent argument
> environment variable
> .agent-collab/rdo.toml
> built-in defaults
```

## rdo.toml

`init_run.py` creates `.agent-collab/rdo.toml` if it does not exist.

```toml
[worker]
backend = "claude-code"
agent_name = "claude-worker"
permission_mode = "auto"
command = ""

[runtime]
backend = "plain" # plain | tmux
io_mode = "machine" # machine | human
startup_timeout_seconds = 45

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

[backends."claude-code"]
disabled_plugins = []
enable_agent_teams = true
max_tool_use_concurrency = 4
enforce_spawn_limit = false

[backends.codex]
enable_multi_agent = true
max_agent_threads = 4
max_agent_depth = 1
enforce_spawn_limit = false

[backends."kimi-code"]
enable_native_subagents = true
enable_agent_swarm = true
max_parallel_subagents = 4
max_agent_depth = 1

[backends.opencode]
enable_native_subagents = true
allowed_subagent_types = ["general", "explore", "scout"]
max_parallel_subagents = 4
max_agent_depth = 1
pure_mode = false
```

The only supported runtime/IO pairs are `plain + machine` and `tmux + human`.
`startup_timeout_seconds` is a positive integer. It limits the wait for the
first valid machine event, or the best-effort TUI prompt-submission startup
sequence. Invalid combinations and invalid startup timeouts fail before
protocol mutation.

`[backends."<id>"]` is durable backend governance for RDO-launched workers,
not an attempt setting and not a user-global CLI setting. Each adapter validates
its own fields. Deny lists are unioned with shipped restrictions, maxima can only
tighten shipped limits, and one-off environment or dispatch arguments cannot
remove these restrictions. The compiled result is stored under the attempt's
`runtime/BACKEND_PROFILE.json`.

Do not add persistent `session_id` to this file. Session id is runtime identity and should be passed with `RDO_BACKEND_SESSION_ID` when available.

Do not add `protocol_version` or `package_version`. Versions are defined by the installed package in the top-level `VERSION` file, written to `RUN.json`, and audited by `collect_status.py`.

## Environment Overrides

```text
RDO_WORKER_COMMAND
CLAUDE_CODE_CMD
RDO_WORKER_BACKEND
RDO_WORKER_AGENT_NAME
CLAUDE_AGENT_NAME
RDO_BACKEND_SESSION_ID
CLAUDE_SESSION_ID
RDO_PERMISSION_MODE
RDO_RUNTIME_BACKEND
RDO_IO_MODE
RDO_STARTUP_TIMEOUT_SECONDS
RDO_TMUX_SESSION_PREFIX
RDO_TMUX_KEEP_SESSION
RDO_TMUX_WAIT_TIMEOUT_SECONDS
RDO_TMUX_EXIT_CODE_GRACE_SECONDS
RDO_STALE_LOCK_HOURS
RDO_STALE_CREATED_MINUTES
RDO_TASK_BRANCH_PREFIX
RDO_WORKTREE_ROOT
```

Boolean env values use the same parser as TOML booleans where applicable: `1/0`, `true/false`, `yes/no`, and `on/off`.

`worker.command`, `RDO_WORKER_COMMAND`, and legacy `CLAUDE_CODE_CMD` are reserved
for isolated RDO test fixtures. Production dispatch rejects command overrides
before lock acquisition because they do not carry a registered permission,
prompt-transport, governance, and startup-event contract. Use the registered
backend adapter and keep credentials in environment variables.

Codex project policy may disable multi-agent or lower the shipped thread/depth
limits. Execution attempts enable Codex multi-agent only when the approved
strategy declares `native_subagents`. Cumulative spawn enforcement is disabled
by default; setting `enforce_spawn_limit = true` activates the machine JSONL
supervisor and consequently requires machine IO.

The shared `permission_mode="auto"` compiles to Codex's **Approve for me**
profile (`on-request` + `workspace-write` + guardian reviewer), not to
`approval_policy=never`. `yolo` remains the only RDO mode that requests Codex's
approval-and-sandbox bypass.

Kimi project policy may disable `Agent` or `AgentSwarm`, and may lower the
parallel or depth limit. RDO launches Kimi with a temporary `KIMI_CODE_HOME`
that combines the user's provider configuration with attempt-local permission
rules and hooks; the user's normal configuration is not edited. Kimi's hook
contract is fail-open, so hook failures are recorded as hard violations rather
than described as an absolute pre-action security boundary.

OpenCode project policy selects the allowed subagent types, parallel bound,
depth bound, and optional `--pure` server mode. RDO launches a per-attempt local
OpenCode server and answers `task` permission requests through its supervisor.
Child sessions without a matching approval, nested child sessions, and excess
concurrency are rejected or aborted and recorded. There is no cumulative launch
counter for Kimi or OpenCode.

If the command path contains spaces, quote it inside TOML as a shell command string, for example:

```toml
[worker]
command = "'/path/with spaces/worker.sh'"
```

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

`dispatch_agent.sh` and its compatibility entrypoint `dispatch_claude.sh` read config defaults through `scripts/config_cli.py export-env --no-env --prefix CONFIG_` before any protocol mutation. Explicit environment variables still win over config defaults.

Do not use unchecked command substitution:

```bash
eval "$(python scripts/config_cli.py export-env)"
```

Capture output and exit status first, then `eval` only on success. Config errors must stop dispatch before locks, attempts, worktrees, or `STATUS -> running` mutations.

Dispatch must still read existing task `branch` and `worktree` only from `STATUS.json`. `task.branch_prefix` and `task.worktree_root` are used only by `create_task.py` when creating a new task.

## Diagnostics

Unknown TOML sections or keys are warnings. Invalid values are errors.

`collect_status.py` reports config warnings under `protocol_warnings` and config errors under `protocol_violations`.

`config_cli.py validate` exits nonzero when config has errors.
