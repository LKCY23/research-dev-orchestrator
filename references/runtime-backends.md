# Runtime Backends

Runtime backend means how `dispatch_agent.sh` launches and supervises a worker CLI. It is separate from worker backend identity.

```text
worker backend  = claude-code | codex | opencode | kimi-code
runtime backend = plain | tmux
io mode         = machine | human
```

## Backends

```text
plain
  Default. Run the worker CLI directly from dispatch_agent.sh.

tmux
  Optional attachable execution. Run the worker CLI inside a tmux session so a human can attach and observe.
```

Use `tmux` only when the user asks for attachable human observation or the worker is expected to run long enough that live inspection matters.

## Supported Matrix

```text
plain + machine  supported
tmux + human     supported
plain + human    rejected before protocol mutation
tmux + machine   rejected before protocol mutation
```

RDO does not silently convert one pair into another. `plain + machine` is the
deterministic automation path. `tmux + human` is the interactive, attachable,
best-effort path.

## Core Boundary

```text
tmux backend = attachable execution, not detached orchestration
```

Dispatch remains synchronous from the protocol perspective:

```text
dispatch starts worker
worker finalizer publishes attempt-bound HANDOFF_READY.json last
attempt supervisor validates that exact bundle and quiesces an interactive process
runner writes exit_code after the process is quiescent
dispatch waits for exit_code
dispatch records worker exit_code
dispatch independently validates against pre-launch expected bindings
dispatch persists the completed ATTEMPT.json result
dispatch applies the terminal STATUS.json transition
dispatch records events
dispatch releases .dispatch-lock only after validation
```

Recognized legacy-v0.5/v1 attempts retain their historical `COMPLETION.json`
publication decoder. It is not a valid v2 signal.

Do not introduce watcher, daemon, queue, RPC, or fire-and-forget semantics into
this runtime boundary.

## Configuration

Project-level defaults may be stored in `.agent-collab/rdo.toml`; see `configuration.md`.

```bash
RDO_WORKER_BACKEND=claude-code|codex|opencode|kimi-code
RDO_RUNTIME_BACKEND=plain|tmux
RDO_IO_MODE=machine|human
RDO_PERMISSION_MODE=default|auto|yolo
RDO_STARTUP_TIMEOUT_SECONDS=45
RDO_TMUX_SESSION_PREFIX=rdo
RDO_TMUX_KEEP_SESSION=0|1
RDO_TMUX_WAIT_TIMEOUT_SECONDS=0
RDO_TMUX_EXIT_CODE_GRACE_SECONDS=60
```

Meanings:

```text
RDO_WORKER_BACKEND
  Selects the CLI agent backend.

RDO_RUNTIME_BACKEND
  plain by default. tmux enables attachable execution.

RDO_IO_MODE
  machine for plain runtime, human for tmux runtime.

RDO_PERMISSION_MODE
  Backend-level permission profile. Unsupported modes fail before protocol mutation.

RDO_STARTUP_TIMEOUT_SECONDS
  Positive startup deadline. Machine mode must emit a valid backend event before
  this deadline. Human mode uses it for best-effort TUI prompt submission.

RDO_TMUX_SESSION_PREFIX
  Prefix for generated tmux session names.

RDO_TMUX_KEEP_SESSION
  false values: 0, false, no, off.
  true values: 1, true, yes, on.
  When false, dispatch may kill/cleanup the tmux session after worker completion.
  When true, runner keeps the tmux session open after worker completion for human review.

RDO_TMUX_WAIT_TIMEOUT_SECONDS
  0: no timeout.
  >0: timeout while waiting for the attempt-local exit_code file.

RDO_TMUX_EXIT_CODE_GRACE_SECONDS
  Grace period for collect_status.py before a tmux exit_code file on a still-running attempt becomes a protocol violation.
```

For dispatch, explicit environment variables override `.agent-collab/rdo.toml`. Invalid config must fail before `.dispatch-lock`, `LOCK`, attempts, worktrees, or `STATUS -> running` mutations.

## Preflight And Startup

Before lock or attempt creation, dispatch validates the requested runtime/IO
pair, executable availability, CLI version invocation, requested permission
mode, command construction, authentication, native resume syntax, and local
session availability when the backend exposes deterministic probes. Capability
checks use the installed CLI's help surface rather than a hard-coded version
table.

Session availability is three-valued:

```text
present  deterministic local storage contains the session
missing  storage was inspected successfully and the session is absent
unknown  storage cannot be inspected authoritatively
```

A missing session does not replace the logical worker. Dispatch records the
requested resume, keeps `worker_id` and attempt lineage, and performs one
explicit full-context start fallback. Claude may reuse the requested UUID with
`--session-id`; Codex confirms the new thread from `thread.started`. An
`unknown` session may be attempted once; a pre-start `session_not_found`
result permits one runtime fallback. For Codex, `thread.started` alone is
session-allocation evidence, not model progress; a deterministic
`session_not_found` rejection before the first non-error model/tool item may
still use that one fallback. No fallback is allowed after
`worker_progress_evidence` exists.

For `plain + machine`, the adapter returns structured `argv`, environment, and
one prompt transport. With `arg`, the prompt appears only in `argv` and stdin is
`/dev/null`; with `stdin`, it appears only in the input stream. Receipt of the
first recognized machine event advances `runtime/STARTUP.json` from
`prompt_dispatched` to `worker_started`. Timeout or early exit becomes
`worker_startup_failed` and blocks the task with `blocker_type=environment`.
Codex also records the first non-error model/tool item separately. A recognized
provider/model rejection after `thread.started` but before that progress is a
startup failure (`model_unavailable` for model access/support errors), not an
execution failure.

For `tmux + human`, startup records TUI process creation and prompt submission.
These are transport observations, not proof that the model acted on the prompt.

## ATTEMPT.runtime Schema

Common fields:

```json
{
  "runtime": {
    "backend": "plain",
    "runtime_backend": "plain",
    "io_mode": "machine",
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
    "runtime_backend": "tmux",
    "io_mode": "human",
    "model": null,
    "cli": "claude",
    "command": "claude",
    "cwd": "/path/to/worktree",
    "tmux_session": "rdo-20260704T1200-T001-A001",
    "attach_command": "tmux attach -t rdo-20260704T1200-T001-A001"
  }
}
```

`runtime.backend`, `runtime.runtime_backend`, `runtime.io_mode`, `runtime.cli`, `runtime.command`, and `runtime.cwd` are required. `runtime.tmux_session` and `runtime.attach_command` are required only when `backend = tmux`.

Generated tmux session names must be sanitized to avoid tmux target separators such as `:`.

## Tmux Publication And Exit Truth

Do not rely only on `tmux wait-for`. It can miss fast signals if dispatch starts waiting after the runner signals completion.

There are two deliberately separate boundaries:

```text
runtime/HANDOFF_READY.json  immutable worker publication requesting quiescence
exit_code                   runner proof that the supervised process has ended
```

The READY marker is valid only when it is under the current active attempt and
its task/attempt IDs, task-input binding, requested state, source commit, and
`HANDOFF.json`/`EVIDENCE.json` digests all validate. It cannot advance the FSM.
Stale, partial, foreign-attempt, or digest-mismatched markers are ignored by the
supervisor. The runner's attempt-local `exit_code` remains the dispatch
synchronization source of truth; full acceptance, worktree, and publication
validation happens again after it exists.

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

Timeout before `exit_code` exists requires explicit cancellation and
reconciliation; it is not permission to abandon a live worker.

On timeout:

```text
dispatch exits 5
cancel the tmux session
record ATTEMPT.state = invalid_handoff
record ATTEMPT.outcome = timed_out_unfinalized
move STATUS to blocked with blocker_type=budget
write diagnostics
release .dispatch-lock after reconciliation
```

Timeout diagnostics should record:

```json
{
  "reason": "tmux_wait_timeout",
  "dispatch_exit_code": 5,
  "worker_exit_code": null,
  "dispatch_lock_retained": false,
  "attempt_cancel_requested": true
}
```

`dispatch_exit_code` remains the exit code of `dispatch_claude.sh`;
`ATTEMPT.exit_code` records the synthetic supervised timeout code `124`.

## Tmux Missing

If `RDO_RUNTIME_BACKEND=tmux` and `tmux` is unavailable, dispatch must fail before creating an attempt, writing `LOCK`, acquiring `.dispatch-lock`, or moving `STATUS.json` to `running`.
