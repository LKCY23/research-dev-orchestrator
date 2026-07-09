# Attempt Lifecycle

`ATTEMPT.json` is the worker execution source of truth for one task attempt. Keep task progress in `STATUS.json`; keep worker/process lifecycle in `ATTEMPT.json`.

## Principles

```text
Task FSM stays about task progress only.
ATTEMPT.json owns worker execution lifecycle.
collect_status.py validates invariants across STATUS, ATTEMPT, LOCK, EVENTS, EVIDENCE, and HANDOFF.
No destructive overwrite; use a new run, new attempt, or revision task.
```

## Dispatch Locking

```text
.dispatch-lock = active dispatch/worker execution mutex
LOCK           = human-readable ownership metadata
```

`dispatch_agent.sh` must acquire `.dispatch-lock` atomically with `mkdir` before starting a worker. The directory contains `attempt_id`, `pid`, and owner metadata so cleanup only removes a lock owned by the current dispatch.

Release `.dispatch-lock` after the worker process exits and handoff validation finishes, including invalid handoff. Keep `LOCK` for audit until Codex review or triage.

If `.dispatch-lock` exists while `STATUS.state` is not `running`, report a protocol violation. A stale execution mutex can block future dispatch even when the task appears ready for review or triage.

## ATTEMPT.json Schema

```json
{
  "attempt_id": "A001-claude-x4p9a",
  "task_id": "T001-name",
  "role": "worker",
  "backend_id": "claude-code",
  "agent": "claude-code",
  "agent_name": "claude-worker-1",
  "backend_session_id": "s8d21",
  "session_id": "s8d21",
  "execution_mode": "start",
  "permission_mode": "auto",
  "state": "completed",
  "handoff_valid": true,
  "handoff_state": "review",
  "started_at": "2026-07-03T12:10:00Z",
  "ended_at": "2026-07-03T12:20:00Z",
  "exit_code": 0,
  "runtime": {
    "backend": "plain",
    "runtime_backend": "plain",
    "io_mode": "machine",
    "model": null,
    "cli": "claude",
    "command": "claude ...",
    "cwd": "/path/to/worktree"
  }
}
```

Schema constraints:

```text
attempt_id: non-empty string
task_id: non-empty string
backend_id: non-empty string, one of supported worker backends
agent: legacy backend alias; non-empty string
agent_name: non-empty string
session_id: string; may be empty only if runtime cannot provide one
state: created|running|completed|invalid_handoff
started_at: non-empty valid ISO timestamp
ended_at: null for created/running; valid ISO timestamp for completed/invalid_handoff
exit_code: null for created/running; integer for completed; integer or null for invalid_handoff
runtime: object
runtime.backend/runtime_backend: plain|tmux
runtime.io_mode: machine|human
runtime.cli: non-empty string
runtime.command: non-empty string
runtime.cwd: non-empty string
runtime.model: optional/null
runtime.tmux_session: required when runtime.backend = tmux
runtime.attach_command: required when runtime.backend = tmux
```

## Attempt States

- `created`: `ATTEMPT.json` exists, but the worker has not been launched yet. This should be brief; stale `created` attempts should be reported as warnings.
- `running`: the worker process is executing.
- `completed`: the worker exited, wrote a valid `HANDOFF.json` request, and dispatch applied the requested terminal task state.
- `invalid_handoff`: the worker exited but did not produce a legal handoff request, evidence bundle, or process result.

If a tmux runner writes an empty or non-integer `exit_code` file, classify the attempt as `invalid_handoff`, set `ended_at`, leave `exit_code = null`, append `worker_exit_without_valid_status`, and release `.dispatch-lock` after diagnostics.

If tmux dispatch times out before the `exit_code` file exists, do not mark the attempt ended. Leave `ATTEMPT.state = running`, `ended_at = null`, and `exit_code = null`; keep `.dispatch-lock` and require Lock Recovery Review.

Do not use attempt state to represent task success. `completed` means the attempt completed protocol handoff, not that the task is approved or merged.

## Handoff Fields

`handoff_valid` must be:

```text
true   when dispatch validated HANDOFF.json and applied STATUS.state to review or blocked
false  when the worker exited without a legal handoff
null   before handoff validation
```

`handoff_state` must be:

```text
review
blocked
null
```

## Task State Invariants

`STATUS.state = running` requires:

```text
current_attempt_id exists
attempts/<current_attempt_id>/ATTEMPT.json exists
ATTEMPT.state in [created, running]
LOCK exists
LOCK.attempt_id == current_attempt_id
.dispatch-lock exists and matches current_attempt_id
.dispatch-lock pid exists, is an integer, and is alive
```

`STATUS.state = review` requires:

```text
ATTEMPT.state = completed
ATTEMPT.handoff_valid = true
ATTEMPT.handoff_state = review
HANDOFF.json exists with _template=false and requested_state=review
STATUS.state_history ends with running -> review by actor dispatch
STATUS.previous_state = running
worker exit_code = 0
EVIDENCE.md has substantive content
HANDOFF.md has substantive content
```

`STATUS.state = blocked` requires:

```text
Either:
  ATTEMPT.state = completed
  ATTEMPT.handoff_valid = true
  ATTEMPT.handoff_state = blocked
  HANDOFF.json exists with _template=false and requested_state=blocked
  HANDOFF.md has substantive content
  worker exit_code may be zero or nonzero
or:
  ATTEMPT.state = invalid_handoff
  ATTEMPT.handoff_valid = false
  blocker_type = needs_coordinator
  blocking_reason explains the invalid handoff
STATUS.state_history ends with running -> blocked by actor dispatch
STATUS.previous_state = running
blocker_type in [needs_coordinator, needs_user, environment, budget, irrecoverable]
blocking_reason non-empty
```

If a worker mutates `STATUS.json` directly, dispatch must not trust that terminal state. It should mark the attempt `invalid_handoff`, move the task to `blocked` with `blocker_type = needs_coordinator`, and leave evidence for coordinator triage.

If `STATUS.state = running` and `ATTEMPT.state` is `completed` or `invalid_handoff`, report a protocol violation. Dispatch normally moves terminal attempts to `review` or `blocked`; a running task with a terminal attempt means supervision or protocol mutation failed.

For `runtime.backend = tmux`, if `attempts/<current_attempt_id>/exit_code` exists while `STATUS.state = running` and `ATTEMPT.state = running`, classify it by supervision evidence:

```text
dispatch pid alive and exit_code age <= grace period:
  report a warning; handoff validation may be in progress

dispatch pid dead, missing, invalid, or exit_code age > grace period:
  report a protocol violation
```

The violation means the tmux runner produced a completion artifact after dispatch supervision was lost or dispatch failed to update `ATTEMPT.json`; Codex must perform Lock Recovery Review.

## No Destructive Overwrite

No command may destructively overwrite or reinitialize audit-bearing artifacts. Updates must be append-only where applicable, or legal state/protocol transitions where mutable.

Audit-bearing artifacts include:

```text
STATUS.json
TASK.md
CONTEXT.md
ACCEPTANCE.md
EVIDENCE.md
HANDOFF.md
attempts/*
EVENTS.ndjson
JOURNAL.md
reviews/*
```

Use a new attempt for implementation retries. Use a revision task such as `T001R1-*` when task scope, acceptance criteria, or design changes.
