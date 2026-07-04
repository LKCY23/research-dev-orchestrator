# Runtime Backends

Worker backend means how `dispatch_claude.sh` launches and supervises the worker CLI. It is not a protocol role and not a terminal application contract.

## Backends

```text
plain
  Default. Run the worker CLI directly from dispatch_claude.sh.

tmux
  Optional attachable execution. Run the worker CLI inside a tmux session so a human can attach and observe.
```

Use `tmux` only when the user asks for attachable/background observation or the worker is expected to run long enough that live inspection matters.

## Core Boundary

```text
tmux backend = attachable execution, not detached orchestration
```

Dispatch remains synchronous from the protocol perspective:

```text
dispatch starts worker
dispatch waits for completion artifact
dispatch records worker exit_code
dispatch validates handoff
dispatch updates ATTEMPT.json and EVENTS.ndjson
dispatch releases .dispatch-lock only after validation
```

Do not introduce watcher, daemon, queue, RPC, or fire-and-forget semantics in the first version.

## Configuration

Project-level defaults may be stored in `.agent-collab/rdo.toml`; see `configuration.md`.

```bash
RDO_WORKER_BACKEND=plain|tmux
RDO_TMUX_SESSION_PREFIX=rdo
RDO_TMUX_KEEP_SESSION=0|1
RDO_TMUX_WAIT_TIMEOUT_SECONDS=0
RDO_TMUX_EXIT_CODE_GRACE_SECONDS=60
```

Meanings:

```text
RDO_WORKER_BACKEND
  plain by default. tmux enables attachable execution.

RDO_TMUX_SESSION_PREFIX
  Prefix for generated tmux session names.

RDO_TMUX_KEEP_SESSION
  0: dispatch may kill/cleanup the tmux session after worker completion.
  1: runner keeps the tmux session open after worker completion for human review.

RDO_TMUX_WAIT_TIMEOUT_SECONDS
  0: no timeout.
  >0: timeout while waiting for the attempt-local exit_code file.

RDO_TMUX_EXIT_CODE_GRACE_SECONDS
  Grace period for collect_status.py before a tmux exit_code file on a still-running attempt becomes a protocol violation.
```

## ATTEMPT.runtime Schema

Common fields:

```json
{
  "runtime": {
    "backend": "plain",
    "model": null,
    "cli": "claude",
    "command": "claude",
    "cwd": "/path/to/worktree"
  }
}
```

For `tmux`:

```json
{
  "runtime": {
    "backend": "tmux",
    "model": null,
    "cli": "claude",
    "command": "claude",
    "cwd": "/path/to/worktree",
    "tmux_session": "rdo-20260704T1200-T001-A001",
    "attach_command": "tmux attach -t rdo-20260704T1200-T001-A001"
  }
}
```

`runtime.backend`, `runtime.cli`, `runtime.command`, and `runtime.cwd` are required. `runtime.tmux_session` and `runtime.attach_command` are required only when `backend = tmux`.

Generated tmux session names must be sanitized to avoid tmux target separators such as `:`.

## Tmux Completion Truth

Do not rely only on `tmux wait-for`. It can miss fast signals if dispatch starts waiting after the runner signals completion.

The completion source of truth is:

```text
attempts/<attempt-id>/exit_code
```

Runner behavior:

```text
always write exit_code file via EXIT trap
optionally signal tmux wait-for for observability
```

Dispatch behavior:

```text
wait until exit_code file exists
read exit_code file
validate handoff
```

During normal completion there is a short window where `exit_code` exists while dispatch is still validating handoff. `collect_status.py` should treat this as a warning only when the dispatch pid is alive and the `exit_code` file is younger than the grace period. It is a protocol violation when the dispatch pid is not alive or the file is older than the grace period while `STATUS` and `ATTEMPT` still report `running`.

If `exit_code` exists but is empty or non-integer:

```text
runner produced invalid completion artifact
worker execution is no longer trusted
ATTEMPT.state = invalid_handoff
ATTEMPT.ended_at = now
ATTEMPT.exit_code = null
append worker_exit_without_valid_status
release .dispatch-lock after diagnostics
dispatch exits 4
```

## Tmux Timeout

Timeout before `exit_code` exists means dispatch lost supervision. It does not prove the worker stopped.

On timeout:

```text
dispatch exits 5
do not validate handoff
do not release .dispatch-lock
leave ATTEMPT.state = running
leave ATTEMPT.ended_at = null
leave ATTEMPT.exit_code = null
write diagnostics
require Lock Recovery Review
```

Timeout diagnostics should record:

```json
{
  "reason": "tmux_wait_timeout",
  "dispatch_exit_code": 5,
  "worker_exit_code": null,
  "dispatch_lock_retained": true
}
```

`dispatch_exit_code` is the exit code of `dispatch_claude.sh`. It must not be written to `ATTEMPT.exit_code`.

## Tmux Missing

If `RDO_WORKER_BACKEND=tmux` and `tmux` is unavailable, dispatch must fail before creating an attempt, writing `LOCK`, acquiring `.dispatch-lock`, or moving `STATUS.json` to `running`.
